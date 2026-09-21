import { expect, type Page } from '@playwright/test'

/**
 * E2E stub 辅助：后端依赖全部在浏览器侧拦截替换，
 * 使用例不依赖 Python runtime / Go 内核进程。
 */

/** 拦截 session 登录并返回假 session */
export async function stubSessionLogin(page: Page, ok = true) {
  await page.route('**/v1/admin/session/login', async (route) => {
    if (!ok) {
      await route.fulfill({
        status: 401,
        contentType: 'application/json',
        body: JSON.stringify({ error: 'unauthorized' }),
      })
      return
    }
    const expires = new Date(Date.now() + 3600_000).toISOString()
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ token: 'e2e-session-token', expires_at: expires }),
    })
  })
}

/** 通用 JSON stub：按路径片段匹配 */
export async function stubJson(page: Page, pathFragment: string, body: unknown, status = 200) {
  await page.route(`**/${pathFragment}**`, async (route) => {
    await route.fulfill({
      status,
      contentType: 'application/json',
      body: JSON.stringify(body),
    })
  })
}

/** 仪表盘与各治理页共用的 python 端 stub（健康/审批/吊销/Agents/审批流 404 兜底/metrics 文本） */
export async function stubBackend(page: Page) {
  await stubJson(page, 'v1/admin/health', {
    status: 'ok',
    opa_reachable: true,
    gateway_ready: true,
    evidence_status: 'ok',
    anchor_status: 'ok',
    persistence: {},
    durability: 'enabled',
    uptime_seconds: 3600,
    harness_backends: [],
  })
  await stubJson(page, 'v1/admin/approvals/pending', { approvals: [] })
  await stubJson(page, 'v1/admin/approvals', { approvals: [], total: 0 })
  await stubJson(page, 'admin/revocation-list', { revocations: [], kill_switch: false })
  await stubJson(page, 'v1/admin/agents', {
    agents: [{ agent_id: 'e2e-agent', name: 'E2E Agent', profile_id: 'default' }],
    users: [],
  })
  // 审批推送端点未就绪时：stub 404 触发前端轮询兜底
  await stubJson(page, 'v1/admin/approvals/stream', { error: 'not_found' }, 404)
  // /metrics 为文本响应
  await page.route('**/metrics**', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'text/plain',
      body: 'lc_requests_total 42\ngo_goroutines 18\n',
    })
  })
}

/** 登录页填写 API Key 并提交 */
export async function loginViaUI(page: Page, apiKey = 'e2e-key') {
  await page.goto('/login')
  await page.getByPlaceholder('请输入 LOOP_CONTROLLER_API_KEY').fill(apiKey)
  await page.getByRole('button', { name: '连接并进入' }).click()
}

/** 断言已完成登录并落在 Dashboard（登录后 / 重定向到 /dashboard） */
export async function expectLoggedIn(page: Page) {
  await expect(page).toHaveURL(/\/dashboard$/)
  await expect(page.getByText('服务状态')).toBeVisible()
}
