import { test, expect, type Page, type Route } from '@playwright/test'
import { stubSessionLogin, stubJson, loginViaUI } from './helpers'

const PENDING_ITEM = {
  decision_id: 'dec-e2e-001',
  request_id: 'req-e2e-001',
  tool_name: 'send_email',
  agent_id: 'researcher_001',
  requester_id: 'user-e2e',
  reason: 'E2E 待审批',
  status: 'pending',
  created_at: '2026-09-17T10:00:00Z',
}

/**
 * 审批操作流 stub：
 * - pending 列表有状态：approvalDone=true 后返回空列表（模拟审批退出队列）；
 * - approve/deny POST 记录请求体与 Bearer 凭证供断言。
 */
async function stubApprovalBackend(page: Page, approveStatus = 200) {
  let approvalDone = false
  const requests: Array<{ action: string; body: any; auth: string | null }> = []

  // 注意：Playwright 路由后注册者优先匹配，审批相关端点用单一处理器按 URL 分发，
  // 避免宽泛的 '**/v1/admin/approvals**' 拦截 /pending 请求
  await page.route('**/v1/admin/approvals**', async (route: Route) => {
    const url = route.request().url()
    if (url.includes('/v1/admin/approvals/pending')) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ approvals: approvalDone ? [] : [PENDING_ITEM] }),
      })
      return
    }
    if (url.includes('/v1/admin/approvals/stream')) {
      await route.fulfill({ status: 404, contentType: 'application/json', body: '{}' })
      return
    }
    const match = url.match(/\/v1\/admin\/approvals\/([^/?]+)\/(approve|deny)/)
    if (match) {
      requests.push({
        action: match[2],
        body: route.request().postDataJSON(),
        auth: route.request().headers()['authorization'] ?? null,
      })
      await route.fulfill({
        status: approveStatus,
        contentType: 'application/json',
        body: JSON.stringify(
          approveStatus === 200
            ? { decision_id: match[1], status: match[2], comment: 'ok' }
            : { detail: 'invalid credential' },
        ),
      })
      if (approveStatus === 200) approvalDone = true
      return
    }
    // 审批历史查询
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        approvals: [{
          ...PENDING_ITEM,
          call_id: 'call-e2e-001',
          task_id: 'task-e2e-001',
          tenant_id: 'tenant-e2e',
          approver_id: 'approver-e2e',
          arguments_masked: { token: '***', recipient: 'safe@example.com' },
          comment: '已核验',
          principal: 'approver-e2e',
          action_summary: '允许发送脱敏邮件',
          original_decision: null,
          status: 'approve',
          decided_at: '2026-09-17T10:01:00Z',
        }],
        total: 1,
        limit: 10,
        offset: 0,
      }),
    })
  })

  return {
    requests,
    /** 等待前端在审批动作后重新拉取 pending（approvalDone 已生效） */
    async expectListDrained() {
      await expect(page.getByText('暂无待审批事项')).toBeVisible()
    },
  }
}

async function loginAndOpenApprovals(page: Page) {
  await stubSessionLogin(page, true)
  // 仪表盘也会拉 pending，同样走上面的 stub
  await stubJson(page, 'v1/admin/health', { status: 'ok' })
  await stubJson(page, 'admin/revocation-list', { revocations: [], kill_switch: false })
  await stubJson(page, 'v1/admin/agents', { agents: [], users: [] })
  await loginViaUI(page)
  await page.getByRole('menuitem', { name: '审批台' }).click()
  await expect(page.getByText('dec-e2e-001')).toBeVisible()
}

test.describe('审批操作流', () => {
  test('通过审批：凭证校验 → 提交 → 列表清空', async ({ page }) => {
    const backend = await stubApprovalBackend(page)
    await loginAndOpenApprovals(page)

    await page.getByRole('button', { name: '通过', exact: true }).first().click()
    await expect(page.getByText('通过审批')).toBeVisible()

    // 未填凭证时校验拦截
    await page.getByRole('button', { name: '确认', exact: true }).click()
    await expect(page.getByText('请输入独立审批凭证')).toBeVisible()

    await page.getByPlaceholder('仅用于本次审批，不会保存').fill('cred-e2e')
    await page.getByPlaceholder('请输入审批意见（拒绝时必须填写）').fill('同意发送')
    await page.getByRole('button', { name: '确认', exact: true }).click()

    await expect(page.getByText('已通过')).toBeVisible()
    await backend.expectListDrained()
    expect(backend.requests).toEqual([
      {
        action: 'approve',
        body: { comment: '同意发送' },
        auth: 'Bearer cred-e2e',
      },
    ])
  })

  test('拒绝审批：意见必填 → 提交后列表清空', async ({ page }) => {
    const backend = await stubApprovalBackend(page)
    await loginAndOpenApprovals(page)

    await page.getByRole('button', { name: '拒绝', exact: true }).first().click()
    await page.getByPlaceholder('仅用于本次审批，不会保存').fill('cred-e2e')
    await page.getByRole('button', { name: '确认', exact: true }).click()
    await expect(page.getByText('拒绝时必须填写审批意见')).toBeVisible()

    await page.getByPlaceholder('请输入审批意见（拒绝时必须填写）').fill('内容不合规')
    await page.getByRole('button', { name: '确认', exact: true }).click()

    await expect(page.getByText('已拒绝')).toBeVisible()
    await backend.expectListDrained()
    expect(backend.requests[0].action).toBe('deny')
    expect(backend.requests[0].body).toEqual({ comment: '内容不合规' })
  })

  test('审批提交失败：错误提示且对话框保留', async ({ page }) => {
    await stubApprovalBackend(page, 403)
    await loginAndOpenApprovals(page)

    await page.getByRole('button', { name: '通过', exact: true }).first().click()
    await page.getByPlaceholder('仅用于本次审批，不会保存').fill('bad-cred')
    await page.getByRole('button', { name: '确认', exact: true }).click()

    await expect(page.getByText('审批失败，请检查独立审批凭证及审批权限后重试')).toBeVisible()
    // 失败不退出对话框，待办仍在列表中
    await expect(page.getByText('通过审批')).toBeVisible()
    await expect(page.getByText('dec-e2e-001')).toBeVisible()
  })

  test('历史详情展示安全字段，不回显原始敏感值', async ({ page }) => {
    await stubApprovalBackend(page)
    await loginAndOpenApprovals(page)

    await page.getByRole('radio', { name: '历史' }).click()
    await page.getByRole('button', { name: '详情' }).last().click()

    await expect(page.getByText('tenant-e2e')).toBeVisible()
    await expect(page.getByText('task-e2e-001')).toBeVisible()
    await expect(page.getByText('call-e2e-001')).toBeVisible()
    await expect(page.getByText('允许发送脱敏邮件')).toBeVisible()
    await expect(page.getByText('***')).toBeVisible()
  })
})

test.describe('吊销与 Kill Switch', () => {
  async function loginAndOpenSettings(page: Page) {
    await stubSessionLogin(page, true)
    await stubJson(page, 'v1/admin/health', { status: 'ok' })
    await stubJson(page, 'v1/admin/entrypoints', { entrypoints: {} })
    await stubJson(page, 'v1/admin/identity', { config: {} })
    await stubJson(page, 'v1/admin/agents', { agents: [], users: [] })
    await stubJson(page, 'v1/admin/approvals/pending', { approvals: [] })
    const calls: Array<{ url: string; body: any }> = []
    await page.route('**/admin/revoke', async (route) => {
      calls.push({ url: route.request().url(), body: route.request().postDataJSON() })
      await route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
    })
    await page.route('**/admin/kill-switch', async (route) => {
      calls.push({ url: route.request().url(), body: route.request().postDataJSON() })
      await route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
    })
    await page.route('**/admin/revocation-list', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ revocations: [], kill_switch: false }),
      })
    })
    await loginViaUI(page)
    await page.getByRole('menuitem', { name: '系统配置' }).click()
    return calls
  }

  test('吊销 Agent：校验 → 提交 → 表单清空', async ({ page }) => {
    const calls = await loginAndOpenSettings(page)

    // 未填完整时校验拦截
    await page.getByRole('button', { name: '吊销', exact: true }).click()
    await expect(page.getByText('请填写完整')).toBeVisible()

    const idInput = page.getByPlaceholder('要吊销的 ID')
    await idInput.fill('agent-evil')
    await page.getByPlaceholder('吊销原因').fill('凭证泄露')
    await page.getByRole('button', { name: '吊销', exact: true }).click()

    await expect(page.getByText('吊销成功')).toBeVisible()
    await expect(idInput).toHaveValue('')
    expect(calls).toContainEqual({
      url: expect.stringContaining('/admin/revoke'),
      body: { type: 'agent', id: 'agent-evil', reason: '凭证泄露' },
    })
  })

  test('Kill Switch：触发需原因 → 触发与解除请求体正确', async ({ page }) => {
    const calls = await loginAndOpenSettings(page)

    // 未填原因不能触发
    await page.getByRole('button', { name: '触发 Kill Switch' }).click()
    await expect(page.getByText('触发 Kill Switch 必须填写原因')).toBeVisible()
    await expect(page.getByText('未触发', { exact: true })).toBeVisible()

    await page.getByPlaceholder('触发 Kill Switch 的原因').fill('发现异常调用')
    await page.getByRole('button', { name: '触发 Kill Switch' }).click()
    await expect(page.getByText('Kill Switch 已触发')).toBeVisible()
    // exact 限定到状态标签，避免与 toast 文案歧义匹配
    await expect(page.getByText('已触发', { exact: true })).toBeVisible()

    await page.getByRole('button', { name: '解除 Kill Switch' }).click()
    await expect(page.getByText('Kill Switch 已解除')).toBeVisible()

    expect(calls.filter((c) => c.url.includes('/admin/kill-switch'))).toEqual([
      { url: expect.stringContaining('/admin/kill-switch'), body: { enabled: true, reason: '发现异常调用' } },
      { url: expect.stringContaining('/admin/kill-switch'), body: { enabled: false, reason: '发现异常调用' } },
    ])
  })
})
