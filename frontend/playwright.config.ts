import { defineConfig, devices } from '@playwright/test'

/**
 * E2E 骨架配置：
 * - 由 Playwright 自动拉起 vite dev server（5173），已有实例则复用；
 * - 全部后端依赖经 page.route stub（见 tests/e2e/helpers.ts），E2E 不依赖
 *   Python runtime / Go 内核进程，可在任意环境运行；
 * - 有后端进程时取消 stub、直连真实环境即为联调模式。
 */
export default defineConfig({
  testDir: './tests/e2e',
  timeout: 30_000,
  retries: 0,
  use: {
    baseURL: 'http://localhost:5173',
    locale: 'zh-CN',
    screenshot: 'only-on-failure',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: {
    command: 'npm run dev',
    url: 'http://localhost:5173',
    reuseExistingServer: true,
    timeout: 60_000,
  },
})
