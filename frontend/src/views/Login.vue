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
import { getHealth, loginAdminSession } from '@/api/python'

const router = useRouter()
const auth = useAuthStore()
const loading = ref(false)

const form = reactive({
  apiKey: auth.apiKey || '',
})

async function handleLogin() {
  if (!form.apiKey.trim()) {
    ElMessage.warning('请输入 API Key')
    return
  }
  loading.value = true
  const apiKey = form.apiKey.trim()
  try {
    auth.login({
      apiKey,
      baseUrl: '/api/python',
      a2aUrl: '/api/a2a',
    })
    // 优先换取 Session Token（后端在线时）；旧后端无该端点则降级为 API Key 直连
    try {
      const session = await loginAdminSession(apiKey)
      auth.setSession(session.token, session.expires_at)
    } catch {
      // 404/网络错误：保持 API Key 模式
    }
    await getHealth()
    ElMessage.success('连接成功')
    router.push('/')
  } catch (error: any) {
    ElMessage.error(error.message || '连接失败，请检查地址和 API Key')
  } finally {
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
