import { defineStore } from 'pinia'
import { ref, computed } from 'vue'

export const useAuthStore = defineStore('auth', () => {
  const apiKey = ref(localStorage.getItem('lc_api_key') || '')
  const baseUrl = ref(localStorage.getItem('lc_base_url') || 'http://127.0.0.1:8000')
  const a2aUrl = ref(localStorage.getItem('lc_a2a_url') || 'http://127.0.0.1:8080')

  const isAuthenticated = computed(() => apiKey.value.length > 0)

  function login(config: { apiKey: string; baseUrl: string; a2aUrl: string }) {
    apiKey.value = config.apiKey
    baseUrl.value = config.baseUrl
    a2aUrl.value = config.a2aUrl
    localStorage.setItem('lc_api_key', config.apiKey)
    localStorage.setItem('lc_base_url', config.baseUrl)
    localStorage.setItem('lc_a2a_url', config.a2aUrl)
  }

  function logout() {
    apiKey.value = ''
    localStorage.removeItem('lc_api_key')
    localStorage.removeItem('lc_base_url')
    localStorage.removeItem('lc_a2a_url')
  }

  return {
    apiKey,
    baseUrl,
    a2aUrl,
    isAuthenticated,
    login,
    logout,
  }
})
