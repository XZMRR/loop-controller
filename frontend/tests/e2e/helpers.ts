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
