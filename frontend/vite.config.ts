import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { resolve } from 'path'
import { readFile } from 'fs/promises'
import type { Connect } from 'vite'

/**
 * 开发模式专用：将 /config/* 映射到后端仓库根目录的 config/，
 * 让前端可以直接读取 agents.yaml / profiles.yaml 等静态配置做展示。
 * 注意：生产部署时不要对外暴露该目录，后端补齐 Admin 配置接口后应移除此中间件。
 */
function serveBackendConfig(): Connect.NextHandleFunction {
  return async (req, res, next) => {
    if (!req.url || !req.url.startsWith('/config/')) {
      next()
      return
    }
    const relPath = req.url.slice('/config/'.length).split('?')[0]
    // 防目录穿越
    if (relPath.includes('..')) {
      res.statusCode = 403
      res.end('forbidden')
      return
    }
    const filePath = resolve(__dirname, '..', 'config', relPath)
    try {
      const content = await readFile(filePath, 'utf-8')
      res.setHeader('Content-Type', 'text/yaml; charset=utf-8')
      res.end(content)
    } catch {
      res.statusCode = 404
      res.end('not found')
    }
  }
}

export default defineConfig({
  plugins: [
    vue(),
    {
      name: 'serve-backend-config',
      configureServer(server) {
        server.middlewares.use(serveBackendConfig())
      },
    },
  ],
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src'),
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api/python': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api\/python/, ''),
      },
      '/api/a2a': {
        target: 'http://127.0.0.1:8080',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api\/a2a/, ''),
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
  },
})
