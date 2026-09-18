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

    <el-row :gutter="16" class="mt-4">
      <el-col :span="6">
        <el-card shadow="hover">
          <div class="stat-title">Go 内核 Readiness</div>
          <div class="stat-value">
            <el-tag :type="kernelReady ? 'success' : 'danger'">
              {{ kernelReady ? '就绪' : '未就绪' }}
            </el-tag>
          </div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="hover" class="clickable" @click="$router.push('/dead-letters')">
          <div class="stat-title">死信</div>
          <div class="stat-value" :class="{ danger: deadLetterCount > 0 }">
            {{ deadLetterCount }}
          </div>
        </el-card>
      </el-col>
    </el-row>

    <el-card class="mt-4" shadow="hover">
      <template #header>
        <div class="card-header">
          <span>关键指标（/metrics）</span>
        </div>
      </template>
      <el-table v-if="metricsRows.length" :data="metricsRows" size="small">
        <el-table-column prop="name" label="指标" min-width="320" show-overflow-tooltip />
        <el-table-column prop="value" label="值" width="160" />
      </el-table>
      <el-empty v-else description="暂无指标数据" />
    </el-card>

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
import { getHealth, getPendingApprovals, getRevocationList, getMetrics } from '@/api/python'
import { fetchKernelReadiness, a2aDeadLetterSource } from '@/api/a2a'
import { loadAgentsConfig } from '@/api/config'
import { usePolling } from '@/composables/useAsyncData'
import type { HealthStatus } from '@/api/python'

const health = ref<HealthStatus | null>(null)
const healthStatus = ref('unknown')
const pendingCount = ref(0)
const agentCount = ref(0)
const killSwitchActive = ref(false)
const kernelReady = ref(false)
const deadLetterCount = ref(0)
const metricsRows = ref<Array<{ name: string; value: string }>>([])
const refreshing = ref(false)
const lastRefreshedAt = ref('')

const DASHBOARD_POLL_INTERVAL = 15000

/** 从 Prometheus 文本中挑选治理/调度相关指标（最多 12 条） */
function parseMetrics(text: string): Array<{ name: string; value: string }> {
  const interesting = /^(lc_|a2a_|go_goroutines)/
  const rows: Array<{ name: string; value: string }> = []
  for (const line of text.split('\n')) {
    if (line.startsWith('#')) continue
    const match = /^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{[^}]*\})?\s+(\S+)$/.exec(line.trim())
    if (!match || !interesting.test(match[1])) continue
    rows.push({ name: match[1], value: match[2] })
    if (rows.length >= 12) break
  }
  return rows
}

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
  // 增强区块独立加载，失败不影响主卡片
  try {
    const [ready, deadLetters, metrics] = await Promise.all([
      fetchKernelReadiness(),
      a2aDeadLetterSource.listDeadLetters(),
      getMetrics(),
    ])
    kernelReady.value = ready
    deadLetterCount.value = deadLetters.length
    metricsRows.value = parseMetrics(metrics)
  } catch {
    // 静默：增强区块不可用时保持现状
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

.clickable {
  cursor: pointer;
}

.stat-value.danger {
  color: #f56c6c;
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
