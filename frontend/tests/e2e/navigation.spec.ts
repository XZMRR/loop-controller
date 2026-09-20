import { test, expect, type Page } from '@playwright/test'
import { stubSessionLogin, stubJson, loginViaUI, expectLoggedIn } from './helpers'

/** 仪表盘与各治理页所需的 python 端 stub（a2a 死信走 Mock 数据源无需 stub） */
async function stubBackend(page: Page) {
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
  // 审批推送为未定契约端点：stub 404 触发前端轮询兜底
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

async function loginWithStubs(page: Page) {
  await stubSessionLogin(page, true)
  await stubBackend(page)
  await loginViaUI(page)
  await expectLoggedIn(page)
}

test.describe('主流程冒烟', () => {
  test('仪表盘四卡片与增强区块渲染', async ({ page }) => {
    await loginWithStubs(page)
    await expect(page.getByText('服务状态')).toBeVisible()
    await expect(page.getByText('待审批')).toBeVisible()
    await expect(page.getByText('Agent 数量')).toBeVisible()
    await expect(page.getByText('Kill Switch')).toBeVisible()
  })

  test('侧边栏菜单导航到各治理页', async ({ page }) => {
    await loginWithStubs(page)

    await page.getByRole('menuitem', { name: '审批台' }).click()
    await expect(page).toHaveURL(/\/approvals$/)
    await expect(page.getByText('审批中心')).toBeVisible()

    await page.getByRole('menuitem', { name: '死信队列' }).click()
    await expect(page).toHaveURL(/\/dead-letters$/)
    // Mock 数据源返回 3 条死信，验证表格列渲染
    await expect(page.getByText('失败类别').first()).toBeVisible()

    await page.getByRole('menuitem', { name: 'RBAC 绑定' }).click()
    await expect(page).toHaveURL(/\/rbac$/)
    // 限定到 Tab 角色：空态文案"暂无角色绑定"会造成歧义匹配
    await expect(page.getByRole('tab', { name: '角色绑定' })).toBeVisible()

    await page.getByRole('menuitem', { name: '策略生命周期' }).click()
    await expect(page).toHaveURL(/\/policies$/)
    await expect(page.getByRole('tab', { name: '策略候选' })).toBeVisible()
  })

  test('未认证直接访问受保护页被重定向', async ({ page }) => {
    await stubBackend(page)
    await page.goto('/policies')
    await expect(page).toHaveURL(/\/login$/)
  })
})
