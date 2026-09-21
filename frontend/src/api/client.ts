import axios from 'axios'
import { ElMessage } from 'element-plus'
import router from '@/router'
import { useAuthStore } from '@/stores/auth'

// 联调模式默认走开发代理；登录页"高级设置"中可自定义直连地址（持久化到 localStorage）
export const DEFAULT_PYTHON_BASE_URL = '/api/python'
export const DEFAULT_A2A_BASE_URL = '/api/a2a'

function resolveBaseUrl(storageKey: string, fallback: string): string {
  return localStorage.getItem(storageKey) || fallback
}

export const PYTHON_URL_STORAGE_KEY = 'lc_python_url'
export const A2A_URL_STORAGE_KEY = 'lc_a2a_url'

export function getPythonBaseUrl(): string {
  return resolveBaseUrl(PYTHON_URL_STORAGE_KEY, DEFAULT_PYTHON_BASE_URL).replace(/\/$/, '')
}

export function buildPythonUrl(path: string): string {
  return `${getPythonBaseUrl()}${path.startsWith('/') ? path : `/${path}`}`
}

export const pythonClient = axios.create({
  baseURL: resolveBaseUrl(PYTHON_URL_STORAGE_KEY, DEFAULT_PYTHON_BASE_URL),
  timeout: 30000,
  headers: {
    'Content-Type': 'application/json',
  },
})

export const a2aClient = axios.create({
  baseURL: resolveBaseUrl(A2A_URL_STORAGE_KEY, DEFAULT_A2A_BASE_URL),
  timeout: 30000,
  headers: {
    'Content-Type': 'application/json',
  },
})

/** 登录页修改地址后调用，使已创建的 client 立即使用新 baseURL。 */
export function applyCustomBaseUrls(): void {
  pythonClient.defaults.baseURL = resolveBaseUrl(PYTHON_URL_STORAGE_KEY, DEFAULT_PYTHON_BASE_URL)
  a2aClient.defaults.baseURL = resolveBaseUrl(A2A_URL_STORAGE_KEY, DEFAULT_A2A_BASE_URL)
}

pythonClient.interceptors.request.use((config) => {
  const auth = useAuthStore()
  if (auth.sessionValid) {
    config.headers['Authorization'] = `Bearer ${auth.sessionToken}`
  }
  return config
})

// 401 统一处理：清理本地凭据并跳回登录页（登录页自身的 401 不跳转，避免循环）
let lastUnauthorizedAt = 0

pythonClient.interceptors.response.use(undefined, (error) => {
  if (error?.response?.status === 401) {
    const auth = useAuthStore()
    auth.logout()
    if (router.currentRoute.value.path !== '/login') {
      const now = Date.now()
      if (now - lastUnauthorizedAt > 3000) {
        lastUnauthorizedAt = now
        ElMessage.warning('登录已失效，请重新登录')
      }
      router.push('/login')
    }
  }
  return Promise.reject(error)
})
