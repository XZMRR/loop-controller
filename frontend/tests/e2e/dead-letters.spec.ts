import { expect, test } from '@playwright/test'
import { stubBackend, stubSessionLogin, loginViaUI, expectLoggedIn } from './helpers'

/** 死信页（Http 数据源）E2E：列表渲染、详情抽屉、重放流程、冲突错误路径。 */
test.describe('死信队列治理页', () => {
  async function loginWithStubs(page: import('@playwright/test').Page) {
    await stubSessionLogin(page)
    await stubBackend(page)
    await loginViaUI(page, 'dl-e2e-key')
    await expectLoggedIn(page)
  }

  const ITEMS = [
    {
      assignment_id: 'assign-e2e-001',
      assignment_kind: 'target_execution',
      tenant_id: 'tenant-e2e',
      task_id: 'task-e2e-001',
      agent_id: 'agent-e2e',
      state: 'dead_letter',
      revision: 3,
      route_attempt: 1,
      attempt: 2,
      replay_count: 0,
      failure_class: 'remote_timeout',
      error_code: 'remote_timeout',
      not_before: null,
      deadline: null,
      outcome: { error_code: 'remote_timeout', message: 'executor unreachable' },
      execution_receipt: { executor: 'exec-e2e', status: 'failed' },
      consumed_budget: { token_count: 1200, payment_amount: 0 },
      created_at: '2026-09-20T10:00:30Z',
      updated_at: '2026-09-20T10:05:00Z',
    },
    {
      assignment_id: 'assign-e2e-002',
      assignment_kind: 'outbound_delegation',
      tenant_id: 'tenant-e2e',
      task_id: 'task-e2e-002',
      agent_id: 'agent-e2e',
      state: 'dead_letter',
      revision: 7,
      route_attempt: 2,
      attempt: 5,
      replay_count: 1,
      failure_class: 'budget_exhausted',
      error_code: 'budget_exhausted',
      not_before: null,
      deadline: null,
      outcome: null,
      execution_receipt: null,
      consumed_budget: { token_count: 5000, payment_amount: 0 },
      created_at: '2026-09-20T11:00:00Z',
      updated_at: '2026-09-20T11:05:00Z',
    },
  ]

  async function stubDeadLetters(
    page: import('@playwright/test').Page,
    items: any[],
    state: { replayed: boolean },
  ) {
    await page.route('**/v1/admin/a2a/dead-letters*', async (route) => {
      if (route.request().method() === 'GET') {
        // 重放成功前端会立即静默刷新，此后返回空列表模拟条目退出死信队列
        const body = state.replayed ? { assignments: [] } : { assignments: items }
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
        return
      }
      await route.continue()
    })
  }

  test('列表渲染、详情抽屉与重放流程', async ({ page }) => {
    await loginWithStubs(page)
    const replayBodies: any[] = []
    const state = { replayed: false }
    await stubDeadLetters(page, ITEMS, state)
    await page.route('**/v1/admin/a2a/dead-letters/*/replay', async (route) => {
      replayBodies.push(route.request().postDataJSON())
      state.replayed = true
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ ...ITEMS[0], state: 'queued', revision: 4, replay_count: 1 }),
      })
    })

    await page.getByRole('menuitem', { name: '死信队列' }).click()
    await expect(page).toHaveURL(/\/dead-letters$/)
    await expect(page.getByRole('button', { name: '刷新' })).toBeVisible()
    await expect(page.getByText('共 2 条死信')).toBeVisible()
    await expect(page.getByText('assign-e2e-001')).toBeVisible()
    await expect(page.getByText('assign-e2e-002')).toBeVisible()
    await expect(page.getByText('remote_timeout').first()).toBeVisible()
    await expect(page.getByText('budget_exhausted')).toBeVisible()

    // 详情抽屉：全字段 + outcome JSON
    await page.getByRole('button', { name: '详情' }).first().click()
    const drawer = page.getByRole('dialog', { name: '死信详情' })
    await expect(drawer).toBeVisible()
    await expect(drawer.getByText('assign-e2e-001')).toBeVisible()
    await expect(drawer.getByText('executor unreachable')).toBeVisible()
    // 关闭抽屉，避免后续断言命中抽屉内重复文本
    await page.keyboard.press('Escape')
    await expect(drawer).toBeHidden()

    // 确认式重放：revision 乐观校验 + tenant_id 请求体
    await page.getByRole('button', { name: '重放' }).first().click()
    await page.getByRole('button', { name: '重放', exact: true }).last().click()
    await expect(page.getByText('重放成功，任务已重新入队')).toBeVisible()
    expect(replayBodies).toEqual([
      { tenant_id: 'tenant-e2e', expected_revision: 3 },
    ])

    // 重放成功后退出列表（第二次轮询返回空）
    await expect(page.getByText('暂无死信——所有任务都在正常调度')).toBeVisible()
    await expect(page.getByText('assign-e2e-001', { exact: true })).toBeHidden()
  })

  test('重放 409 冲突：错误提示且条目保留', async ({ page }) => {
    await loginWithStubs(page)
    const state = { replayed: false }
    await stubDeadLetters(page, ITEMS, state)
    await page.route('**/v1/admin/a2a/dead-letters/*/replay', async (route) => {
      await route.fulfill({
        status: 409,
        contentType: 'application/json',
        body: JSON.stringify({ error: 'dead letter replay conflict' }),
      })
    })

    await page.getByRole('menuitem', { name: '死信队列' }).click()
    await expect(page).toHaveURL(/\/dead-letters$/)
    await expect(page.getByText('assign-e2e-001')).toBeVisible()

    await page.getByRole('button', { name: '重放' }).first().click()
    await page.getByRole('button', { name: '重放', exact: true }).last().click()
    await expect(page.getByText('Request failed with status code 409')).toBeVisible()
    await expect(page.getByText('assign-e2e-001', { exact: true })).toBeVisible()
  })

  test('列表加载失败：错误态与重试恢复', async ({ page }) => {
    await loginWithStubs(page)
    let failed = true
    await page.route('**/v1/admin/a2a/dead-letters*', async (route) => {
      if (!failed) {
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ assignments: ITEMS }) })
        return
      }
      await route.fulfill({ status: 502, contentType: 'application/json', body: JSON.stringify({ error: 'go kernel dead-letter list failed' }) })
    })

    await page.getByRole('menuitem', { name: '死信队列' }).click()
    await expect(page).toHaveURL(/\/dead-letters$/)
    await expect(page.getByText('数据加载失败')).toBeVisible()
    await expect(page.getByText('Request failed with status code 502')).toBeVisible()

    failed = false
    await page.getByRole('button', { name: '重试' }).click()
    await expect(page.getByText('assign-e2e-001')).toBeVisible()
  })
})
