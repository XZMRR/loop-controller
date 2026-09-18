import { test, expect, type Page } from '@playwright/test'
import { stubSessionLogin, stubJson, loginViaUI } from './helpers'

async function loginAndBreakApprovals(page: Page) {
  await stubSessionLogin(page, true)
  // 待审批列表返回 500，触发错误态
  await stubJson(page, 'v1/admin/approvals/pending', { error: 'backend down' }, 500)
  await stubJson(page, 'v1/admin/approvals/stream', { error: 'not_found' }, 404)
  await loginViaUI(page)
  await page.getByRole('menuitem', { name: '审批台' }).click()
}

test.describe('三态规范：错误态', () => {
  test('列表加载失败展示错误块与重试按钮', async ({ page }) => {
    await loginAndBreakApprovals(page)
    await expect(page.getByText('数据加载失败')).toBeVisible()
    await expect(page.getByRole('button', { name: '重试' })).toBeVisible()
    // axios 错误消息透传（HTTP 500）
    await expect(page.getByText(/加载审批失败|Request failed with status code 500/)).toBeVisible()
  })

  test('重试成功后错误块消失', async ({ page }) => {
    await loginAndBreakApprovals(page)
    await expect(page.getByText('数据加载失败')).toBeVisible()

    // 解除故障后重试：页面恢复空态
    await page.unrouteAll()
    await stubSessionLogin(page, true)
    await stubJson(page, 'v1/admin/approvals/pending', { approvals: [] })
    await page.getByRole('button', { name: '重试' }).click()

    await expect(page.getByText('数据加载失败')).not.toBeVisible()
    await expect(page.getByText('暂无待审批事项')).toBeVisible()
  })
})
