import { test, expect, type Page } from '@playwright/test'
import { stubSessionLogin, stubBackend, loginViaUI, expectLoggedIn } from './helpers'

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
