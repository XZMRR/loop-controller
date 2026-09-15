import { defineStore } from 'pinia'
import { ref, computed } from 'vue'

export const useAuthStore = defineStore('auth', () => {
  const apiKey = ref(localStorage.getItem('lc_api_key') || '')
  const baseUrl = ref(localStorage.getItem('lc_base_url') || 'http://127.0.0.1:8000')
  const a2aUrl = ref(localStorage.getItem('lc_a2a_url') || 'http://127.0.0.1:8080')
  // Session 登录（后端签发）：优先使用，避免前端长期持有明文 API Key
  const sessionToken = ref(localStorage.getItem('lc_session_token') || '')
  const sessionExpiresAt = ref(localStorage.getItem('lc_session_expires') || '')

  const sessionValid = computed(() => {
    if (!sessionToken.value) return false
    if (!sessionExpiresAt.value) return true
    return new Date(sessionExpiresAt.value).getTime() > Date.now()
  })

  const isAuthenticated = computed(
    () => apiKey.value.length > 0 || sessionValid.value,
  )

  function login(config: { apiKey: string; baseUrl: string; a2aUrl: string }) {
    apiKey.value = config.apiKey
    baseUrl.value = config.baseUrl
    a2aUrl.value = config.a2aUrl
    localStorage.setItem('lc_api_key', config.apiKey)
    localStorage.setItem('lc_base_url', config.baseUrl)
    localStorage.setItem('lc_a2a_url', config.a2aUrl)
  }

  function setSession(token: string, expiresAt: string) {
    sessionToken.value = token
    sessionExpiresAt.value = expiresAt
    localStorage.setItem('lc_session_token', token)
    localStorage.setItem('lc_session_expires', expiresAt)
  }

  function logout() {
    apiKey.value = ''
    sessionToken.value = ''
    sessionExpiresAt.value = ''
    localStorage.removeItem('lc_api_key')
    localStorage.removeItem('lc_base_url')
    localStorage.removeItem('lc_a2a_url')
    localStorage.removeItem('lc_session_token')
    localStorage.removeItem('lc_session_expires')
  }

  return {
    apiKey,
    baseUrl,
    a2aUrl,
    sessionToken,
    sessionExpiresAt,
    sessionValid,
    isAuthenticated,
    login,
    setSession,
    logout,
  }
})
