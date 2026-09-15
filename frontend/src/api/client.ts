import axios from 'axios'
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
  if (auth.apiKey) {
    config.headers['X-API-Key'] = auth.apiKey
  }
  return config
})

a2aClient.interceptors.request.use((config) => {
  const auth = useAuthStore()
  if (auth.apiKey) {
    config.headers['Authorization'] = `Bearer ${auth.apiKey}`
  }
  return config
})
