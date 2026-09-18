import { test, expect } from '@playwright/test'
import { stubSessionLogin, loginViaUI, expectLoggedIn } from './helpers'

test.describe('登录页', () => {
  test('渲染登录表单', async ({ page }) => {
    await page.goto('/login')
    await expect(page.getByRole('heading', { name: 'Loop Controller Console' })).toBeVisible()
    await expect(page.getByPlaceholder('请输入 LOOP_CONTROLLER_API_KEY')).toBeVisible()
    await expect(page.getByRole('button', { name: '连接并进入' })).toBeVisible()
  })

  test('未认证访问受保护路由会重定向到登录页', async ({ page }) => {
    await page.goto('/approvals')
    await expect(page).toHaveURL(/\/login$/)
  })

  test('空 API Key 提交提示输入', async ({ page }) => {
    await page.goto('/login')
    await page.getByRole('button', { name: '连接并进入' }).click()
    await expect(page.getByText('请输入 API Key')).toBeVisible()
    await expect(page).toHaveURL(/\/login$/)
  })

  test('登录失败停留在登录页并提示', async ({ page }) => {
    await stubSessionLogin(page, false)
    await loginViaUI(page, 'bad-key')
    await expect(page.getByText('登录失败，请检查 API Key')).toBeVisible()
    await expect(page).toHaveURL(/\/login$/)
  })

  test('登录成功进入仪表盘', async ({ page }) => {
    await stubSessionLogin(page, true)
    await loginViaUI(page)
    await expectLoggedIn(page)
  })
})
