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
import { loginAdminSession } from '@/api/python'

const router = useRouter()
const auth = useAuthStore()
const loading = ref(false)

const form = reactive({
  apiKey: '',
})

async function handleLogin() {
  if (!form.apiKey.trim()) {
    ElMessage.warning('请输入 API Key')
    return
  }
  loading.value = true
  try {
    const session = await loginAdminSession(form.apiKey.trim())
    auth.setSession(session.token, session.expires_at)
    auth.saveUrls('/api/python', '/api/a2a')
    await router.push('/')
    ElMessage.success('连接成功')
  } catch (error: any) {
    auth.logout()
    ElMessage.error(error.message || '登录失败，请检查 API Key')
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
