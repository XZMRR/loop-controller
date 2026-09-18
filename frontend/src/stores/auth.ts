import { defineStore } from 'pinia'
import { ref, computed } from 'vue'

export const useAuthStore = defineStore('auth', () => {
  localStorage.removeItem('lc_api_key')
  localStorage.removeItem('lc_session_token')
  localStorage.removeItem('lc_session_expires')

  const baseUrl = ref(localStorage.getItem('lc_base_url') || 'http://127.0.0.1:8000')
  const a2aUrl = ref(localStorage.getItem('lc_a2a_url') || 'http://127.0.0.1:8080')
  const sessionToken = ref('')
  const sessionExpiresAt = ref('')
  let expiryTimer: ReturnType<typeof setTimeout> | undefined

  const sessionValid = computed(() =>
    !!sessionToken.value && new Date(sessionExpiresAt.value).getTime() > Date.now(),
  )
  const isAuthenticated = computed(() => sessionValid.value)

  function setSession(token: string, expiresAt: string) {
    const remaining = new Date(expiresAt).getTime() - Date.now()
    if (!token || !Number.isFinite(remaining) || remaining <= 0) {
      throw new Error('无效的 Session，请重新登录')
    }
    if (expiryTimer) clearTimeout(expiryTimer)
    sessionToken.value = token
    sessionExpiresAt.value = expiresAt
    function scheduleExpiry() {
      const timeLeft = new Date(sessionExpiresAt.value).getTime() - Date.now()
      if (timeLeft > 0) {
        expiryTimer = setTimeout(scheduleExpiry, Math.min(timeLeft, 2147483647))
      } else {
        logout()
        void import('@/router').then(({ default: router }) => router.replace('/login'))
      }
    }
    scheduleExpiry()
  }

  function saveUrls(base: string, a2a: string) {
    baseUrl.value = base
    a2aUrl.value = a2a
    localStorage.setItem('lc_base_url', base)
    localStorage.setItem('lc_a2a_url', a2a)
  }

  function logout() {
    if (expiryTimer) clearTimeout(expiryTimer)
    expiryTimer = undefined
    sessionToken.value = ''
    sessionExpiresAt.value = ''
    localStorage.removeItem('lc_api_key')
    localStorage.removeItem('lc_session_token')
    localStorage.removeItem('lc_session_expires')
  }

  return {
    baseUrl,
    a2aUrl,
    sessionToken,
    sessionExpiresAt,
    sessionValid,
    isAuthenticated,
    saveUrls,
    setSession,
    logout,
  }
})
