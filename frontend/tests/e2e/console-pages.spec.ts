import { test, expect, type Page } from '@playwright/test'
import { stubSessionLogin, stubBackend, stubJson, loginViaUI, expectLoggedIn } from './helpers'

/**
 * 治理台页面可用性红线（E2E 层）。
 *
 * 回归对象：v0.55 浏览器人工点验发现 RBAC 绑定页 503、策略生命周期页 503、
 * A2A 治理页 502（"数据加载失败"/"Request failed"横幅）。后端装配红线由
 * tests/test_admin_console_availability.py 锁定，本文件锁定前端渲染层：
 * 后端正常时页面必须加载数据、不得出现错误横幅。
 */

async function stubConsolePages(page: Page) {
  // 兜底（最低优先级，先注册）：未逐一 stub 的管理端点返回 200，避免请求穿透到
  // 真实后端拿到 401 把内存会话踢回登录页。后注册的具体 stub 优先匹配。
  await page.route('**/admin/**', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
  })
  await stubSessionLogin(page, true)
  await stubBackend(page)
  // RBAC 绑定页
  await stubJson(page, 'v1/admin/rbac/bindings', { bindings: [] })
  await stubJson(page, 'v1/admin/rbac/grants', { grants: [] })
  // 策略生命周期页
  await stubJson(page, 'v1/admin/policy/candidates', { candidates: [] })
  await stubJson(page, 'v1/admin/policy/status', {
    bundle_name: 'loop-controller',
    generation: 0,
    instances: [],
  })
  await stubJson(page, 'v1/admin/policy/audit', { events: [] })
  // A2A 治理页
  await stubJson(page, 'v1/admin/a2a/status', {
    enabled: true,
    reachable: true,
    base_url: 'http://127.0.0.1:8080',
    local_agent: { agent_id: 'loop-controller-local' },
  })
  await stubJson(page, 'v1/admin/a2a/agents', {
    agents: [
      {
        agent_id: 'e2e-agent',
        name: 'E2E Agent',
        profile_id: 'research_assistant_v1',
        owner: 'e2e-owner',
        kernel_registered: true,
      },
    ],
  })
}

async function loginAndOpen(page: Page, menuName: string) {
  await stubConsolePages(page)
  await loginViaUI(page)
  await expectLoggedIn(page)
  await page.getByRole('menuitem', { name: menuName }).click()
}

test.describe('治理台页面可用性红线', () => {
  test('RBAC 绑定页：数据加载成功且无错误横幅', async ({ page }) => {
    await loginAndOpen(page, 'RBAC 绑定')
    await expect(page).toHaveURL(/\/rbac$/)
    await expect(page.getByRole('tab', { name: '角色绑定' })).toBeVisible()
    await expect(page.getByText('暂无角色绑定')).toBeVisible()
    await expect(page.getByText('数据加载失败')).not.toBeVisible()
  })

  test('策略生命周期页：数据加载成功且无错误横幅', async ({ page }) => {
    await loginAndOpen(page, '策略生命周期')
    await expect(page).toHaveURL(/\/policies$/)
    await expect(page.getByRole('tab', { name: '策略候选' })).toBeVisible()
    await expect(page.getByText('暂无策略候选')).toBeVisible()
    await expect(page.getByText('数据加载失败')).not.toBeVisible()
  })

  test('A2A 治理页：内核可达渲染且无错误横幅', async ({ page }) => {
    await loginAndOpen(page, 'A2A 治理')
    await expect(page).toHaveURL(/\/a2a$/)
    await expect(page.getByText('内核可达')).toBeVisible()
    await expect(page.getByText('可达').first()).toBeVisible()
    await expect(page.getByText('数据加载失败')).not.toBeVisible()
  })
})
