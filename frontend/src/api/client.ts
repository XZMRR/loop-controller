import axios from 'axios'
import { ElMessage } from 'element-plus'
import router from '@/router'
import { useAuthStore } from '@/stores/auth'

export const pythonClient = axios.create({
  baseURL: '/api/python',
  timeout: 30000,
  headers: {
    'Content-Type': 'application/json',
  },
})

export const a2aClient = axios.create({
  baseURL: '/api/a2a',
  timeout: 30000,
  headers: {
    'Content-Type': 'application/json',
  },
})

pythonClient.interceptors.request.use((config) => {
  const auth = useAuthStore()
  if (auth.sessionValid) {
    config.headers['Authorization'] = `Bearer ${auth.sessionToken}`
  }
  return config
})

a2aClient.interceptors.request.use((config) => {
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
