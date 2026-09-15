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
        <span>运行指标</span>
      </template>
      <pre v-if="health">{{ JSON.stringify(health, null, 2) }}</pre>
      <el-empty v-else description="暂无数据" />
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { getHealth, getPendingApprovals, getRevocationList } from '@/api/python'
import { loadAgentsConfig } from '@/api/config'
import type { HealthStatus } from '@/api/python'

const health = ref<HealthStatus | null>(null)
const healthStatus = ref('unknown')
const pendingCount = ref(0)
const agentCount = ref(0)
const killSwitchActive = ref(false)

async function loadData() {
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
    killSwitchActive.value = revocation.kill_switch?.active || false
  } catch (error: any) {
    ElMessage.error(error.message || '加载仪表盘失败')
  }
}

onMounted(loadData)
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
</style>
