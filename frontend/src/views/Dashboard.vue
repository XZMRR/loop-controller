<template>
  <div>
    <el-row :gutter="16">
      <el-col :span="6">
        <el-card shadow="hover">
          <div class="stat-title">服务状态</div>
          <div class="stat-value">
            <el-tag :type="healthStatus === 'ok' ? 'success' : 'danger'">
              {{ healthStatus === 'ok' ? '正常' : '异常' }}
            </el-tag>
          </div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="hover">
          <div class="stat-title">待审批</div>
          <div class="stat-value">{{ pendingCount }}</div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="hover">
          <div class="stat-title">Agent 数量</div>
          <div class="stat-value">{{ agentCount }}</div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="hover">
          <div class="stat-title">Kill Switch</div>
          <div class="stat-value">
            <el-tag :type="killSwitchActive ? 'danger' : 'info'">
              {{ killSwitchActive ? '已触发' : '未触发' }}
            </el-tag>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <el-card class="mt-4" shadow="hover">
      <template #header>
        <div class="card-header">
          <span>运行指标</span>
          <span class="header-actions">
            <span v-if="lastRefreshedAt" class="last-refreshed">
              上次刷新 {{ lastRefreshedAt }}
            </span>
            <el-button size="small" :loading="refreshing" @click="refreshNow">刷新</el-button>
          </span>
        </div>
      </template>
      <div v-loading="refreshing && !health">
        <pre v-if="health">{{ JSON.stringify(health, null, 2) }}</pre>
        <el-empty v-else description="暂无数据" />
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { getHealth, getPendingApprovals, getRevocationList } from '@/api/python'
import { loadAgentsConfig } from '@/api/config'
import { usePolling } from '@/composables/useAsyncData'
import type { HealthStatus } from '@/api/python'

const health = ref<HealthStatus | null>(null)
const healthStatus = ref('unknown')
const pendingCount = ref(0)
const agentCount = ref(0)
const killSwitchActive = ref(false)
const refreshing = ref(false)
const lastRefreshedAt = ref('')

const DASHBOARD_POLL_INTERVAL = 15000

async function loadData(silent = false) {
  refreshing.value = true
  try {
    const [healthData, pending, agents, revocation] = await Promise.all([
      getHealth(),
      getPendingApprovals(),
      loadAgentsConfig(),
      getRevocationList(),
    ])
    health.value = healthData
    healthStatus.value = healthData.status
    pendingCount.value = pending.length
    agentCount.value = agents.agents?.length || 0
    killSwitchActive.value = revocation.kill_switch?.enabled || false
    lastRefreshedAt.value = new Date().toLocaleTimeString()
  } catch (error: any) {
    if (!silent) {
      ElMessage.error(error.message || '加载仪表盘失败')
    }
  } finally {
    refreshing.value = false
  }
}

function refreshNow() {
  void loadData(false)
}

// 轮询：静默刷新，页面不可见时自动跳过（composable 内部处理）
usePolling(() => loadData(true), DASHBOARD_POLL_INTERVAL)

onMounted(() => loadData(true))
</script>

<style scoped>
.stat-title {
  color: #909399;
  font-size: 14px;
  margin-bottom: 8px;
}

.stat-value {
  font-size: 28px;
  font-weight: bold;
}

.mt-4 {
  margin-top: 16px;
}

.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}

.last-refreshed {
  color: #909399;
  font-size: 12px;
}
</style>
