<template>
  <div class="login-container">
    <el-card class="login-card" shadow="hover">
      <template #header>
        <div class="login-header">
          <el-icon size="32"><Monitor /></el-icon>
          <h2>Loop Controller Console</h2>
          <p>Agent 工具调用治理控制台</p>
        </div>
      </template>
      <el-form :model="form" label-position="top" @submit.prevent="handleLogin">
        <el-alert
          title="当前为联调模式：后端地址由开发代理固定，生产环境建议由网关统一代理。"
          type="info"
          :closable="false"
          style="margin-bottom: 16px"
        />
        <el-form-item label="Admin API Key">
          <el-input
            v-model="form.apiKey"
            type="password"
            show-password
            placeholder="请输入 LOOP_CONTROLLER_API_KEY"
          />
        </el-form-item>
        <el-form-item>
          <el-checkbox v-model="showAdvanced">自定义后端地址（默认走开发代理）</el-checkbox>
        </el-form-item>
        <template v-if="showAdvanced">
          <el-form-item label="Python Runtime 地址">
            <el-input v-model="form.pythonUrl" placeholder="/api/python 或 http://127.0.0.1:8000" />
          </el-form-item>
          <el-form-item label="Go A2A 地址">
            <el-input v-model="form.a2aUrl" placeholder="/api/a2a 或 http://127.0.0.1:8080" />
          </el-form-item>
        </template>
        <el-button type="primary" native-type="submit" :loading="loading" style="width: 100%">
          连接并进入
        </el-button>
      </el-form>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '@/stores/auth'
import {
  applyCustomBaseUrls,
  A2A_URL_STORAGE_KEY,
  DEFAULT_A2A_BASE_URL,
  DEFAULT_PYTHON_BASE_URL,
  PYTHON_URL_STORAGE_KEY,
} from '@/api/client'
import { loginAdminSession } from '@/api/python'

const router = useRouter()
const auth = useAuthStore()
const loading = ref(false)

// 自定义地址：留空表示使用默认开发代理；填写后持久化，下次自动回填
const savedPythonUrl = localStorage.getItem(PYTHON_URL_STORAGE_KEY) || ''
const savedA2aUrl = localStorage.getItem(A2A_URL_STORAGE_KEY) || ''
const showAdvanced = ref(Boolean(savedPythonUrl || savedA2aUrl))

const form = reactive({
  // v0.54 安全边界：API Key 不预填、不持久化，仅用于换取 Session
  apiKey: '',
  pythonUrl: savedPythonUrl,
  a2aUrl: savedA2aUrl,
})

function persistCustomUrls() {
  const pythonUrl = form.pythonUrl.trim()
  const a2aUrl = form.a2aUrl.trim()
  if (pythonUrl) localStorage.setItem(PYTHON_URL_STORAGE_KEY, pythonUrl)
  else localStorage.removeItem(PYTHON_URL_STORAGE_KEY)
  if (a2aUrl) localStorage.setItem(A2A_URL_STORAGE_KEY, a2aUrl)
  else localStorage.removeItem(A2A_URL_STORAGE_KEY)
  applyCustomBaseUrls()
}

async function handleLogin() {
  if (!form.apiKey.trim()) {
    ElMessage.warning('请输入 API Key')
    return
  }
  loading.value = true
  try {
    persistCustomUrls()
    const session = await loginAdminSession(form.apiKey.trim())
    auth.setSession(session.token, session.expires_at)
    auth.saveUrls(
      form.pythonUrl.trim() || DEFAULT_PYTHON_BASE_URL,
      form.a2aUrl.trim() || DEFAULT_A2A_BASE_URL,
    )
    await router.push('/')
    ElMessage.success('连接成功')
  } catch (error: any) {
    auth.logout()
    const message =
      error?.response?.status === 401
        ? '登录失败，请检查 API Key'
        : error?.message || '登录失败，请检查 API Key'
    ElMessage.error(message)
  } finally {
    form.apiKey = ''
    loading.value = false
  }
}
</script>

<style scoped>
.login-container {
  height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background-color: #f5f7fa;
}

.login-card {
  width: 420px;
}

.login-header {
  text-align: center;
}

.login-header h2 {
  margin: 12px 0 4px;
}

.login-header p {
  margin: 0;
  color: #909399;
  font-size: 14px;
}
</style>
