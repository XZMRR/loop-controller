<template>
  <div>
    <el-card>
      <div class="toolbar">
        <div class="toolbar-left">
          <span class="label">租户</span>
          <el-select
            v-model="tenantFilter"
            placeholder="全部租户"
            clearable
            style="width: 180px"
            @change="refresh()"
          >
            <el-option
              v-for="tenant in tenantOptions"
              :key="tenant"
              :label="tenant"
              :value="tenant"
            />
          </el-select>
          <el-tag v-if="data" type="info" effect="plain">
            共 {{ filteredList.length }} 条死信
          </el-tag>
        </div>
        <el-button :icon="Refresh" @click="refresh()" :loading="loading">
          刷新
        </el-button>
      </div>

      <ErrorState
        v-if="!loading && error"
        :message="error"
        @retry="refresh()"
      />
      <el-table v-loading="loading" :data="filteredList" style="width: 100%">
        <el-table-column prop="assignment_id" label="Assignment ID" min-width="170" show-overflow-tooltip />
        <el-table-column prop="task_id" label="任务" min-width="160" show-overflow-tooltip />
        <el-table-column prop="agent_id" label="目标 Agent" min-width="130" show-overflow-tooltip />
        <el-table-column label="失败类别" min-width="150">
          <template #default="{ row }">
            <el-tag :type="failureTagType(row.failure_class)" effect="plain">
              {{ row.failure_class }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="尝试 / 重放" width="110" align="center">
          <template #default="{ row }">
            {{ row.attempt }} / {{ row.replay_count }}
          </template>
        </el-table-column>
        <el-table-column label="Not Before" min-width="160">
          <template #default="{ row }">
            {{ row.not_before ? formatTime(row.not_before) : '—' }}
          </template>
        </el-table-column>
        <el-table-column label="更新时间" min-width="160">
          <template #default="{ row }">
            {{ formatTime(row.updated_at) }}
          </template>
        </el-table-column>
        <el-table-column label="操作" width="150" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="openDetail(row)">详情</el-button>
            <el-button link type="warning" @click="confirmReplay(row)">重放</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="暂无死信——所有任务都在正常调度" />
        </template>
      </el-table>
    </el-card>

    <el-drawer v-model="detailVisible" title="死信详情" size="480px">
      <template v-if="detail">
        <el-descriptions :column="1" border>
          <el-descriptions-item label="Assignment ID">
            {{ detail.assignment_id }}
          </el-descriptions-item>
          <el-descriptions-item label="类型">
            {{ detail.assignment_kind }}
          </el-descriptions-item>
          <el-descriptions-item label="租户">{{ detail.tenant_id }}</el-descriptions-item>
          <el-descriptions-item label="任务">{{ detail.task_id }}</el-descriptions-item>
          <el-descriptions-item label="目标 Agent">{{ detail.agent_id }}</el-descriptions-item>
          <el-descriptions-item label="状态">
            <el-tag type="danger" effect="plain">{{ detail.state }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="失败类别">
            <el-tag :type="failureTagType(detail.failure_class)" effect="plain">
              {{ detail.failure_class }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item v-if="detail.error_code" label="错误码">
            {{ detail.error_code }}
          </el-descriptions-item>
          <el-descriptions-item label="Revision">{{ detail.revision }}</el-descriptions-item>
          <el-descriptions-item label="路由尝试">{{ detail.route_attempt }}</el-descriptions-item>
          <el-descriptions-item label="执行尝试">{{ detail.attempt }}</el-descriptions-item>
          <el-descriptions-item label="已重放次数">{{ detail.replay_count }}</el-descriptions-item>
          <el-descriptions-item label="Not Before">
            {{ detail.not_before ? formatTime(detail.not_before) : '—' }}
          </el-descriptions-item>
          <el-descriptions-item label="Deadline">
            {{ detail.deadline ? formatTime(detail.deadline) : '—' }}
          </el-descriptions-item>
          <el-descriptions-item v-if="detail.consumed_budget" label="已耗预算">
            {{ detail.consumed_budget.token_count ?? 0 }} tokens
          </el-descriptions-item>
          <el-descriptions-item label="创建时间">{{ formatTime(detail.created_at) }}</el-descriptions-item>
          <el-descriptions-item label="更新时间">{{ formatTime(detail.updated_at) }}</el-descriptions-item>
        </el-descriptions>
        <template v-if="detail.outcome">
          <div class="section-title">Outcome</div>
          <pre class="json-block">{{ JSON.stringify(detail.outcome, null, 2) }}</pre>
        </template>
        <template v-if="detail.execution_receipt">
          <div class="section-title">Execution Receipt</div>
          <pre class="json-block">{{ JSON.stringify(detail.execution_receipt, null, 2) }}</pre>
        </template>
      </template>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { a2aDeadLetterSource, type A2ADeadLetterItem } from '@/api/a2a'
import { useAsyncData, usePolling } from '@/composables/useAsyncData'
import ErrorState from '@/components/ErrorState.vue'

const {
  data,
  loading,
  error,
  refresh,
} = useAsyncData(() => a2aDeadLetterSource.listDeadLetters(), {
  defaultErrorMessage: '死信列表加载失败',
  // 加载失败由 ErrorState 持久展示，不再弹 toast
  showError: false,
})

usePolling(() => refresh(true), 15000)

const tenantFilter = ref('')
const filteredList = computed<A2ADeadLetterItem[]>(() => {
  if (!data.value) return []
  if (!tenantFilter.value) return data.value
  return data.value.filter((item) => item.tenant_id === tenantFilter.value)
})

const tenantOptions = computed(() => {
  const tenants = new Set((data.value ?? []).map((item) => item.tenant_id))
  return [...tenants]
})

const detailVisible = ref(false)
const detail = ref<A2ADeadLetterItem | null>(null)

function openDetail(row: A2ADeadLetterItem) {
  detail.value = row
  detailVisible.value = true
}

function failureTagType(failureClass: string): 'success' | 'warning' | 'danger' | 'info' {
  switch (failureClass) {
    case 'remote_timeout':
    case 'sent_unacknowledged':
      return 'warning'
    case 'pre_dispatch_transient':
      return 'info'
    default:
      return 'danger'
  }
}

function formatTime(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

async function confirmReplay(row: A2ADeadLetterItem) {
  try {
    await ElMessageBox.confirm(
      `确认重放死信 ${row.assignment_id}？` +
        `任务将重新入队调度（revision ${row.revision} → ${row.revision + 1}）。`,
      '重放死信',
      { type: 'warning', confirmButtonText: '重放', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await a2aDeadLetterSource.replayDeadLetter(row.assignment_id, {
      tenant_id: row.tenant_id,
      expected_revision: row.revision,
    })
    ElMessage.success('重放成功，任务已重新入队')
    await refresh(true)
  } catch (e: any) {
    ElMessage.error(e?.message || '重放失败')
  }
}
</script>

<style scoped>
.toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
}

.toolbar-left {
  display: flex;
  align-items: center;
  gap: 12px;
}

.label {
  font-size: 14px;
  color: #606266;
}

.section-title {
  margin: 16px 0 8px;
  font-size: 14px;
  font-weight: 600;
}

.json-block {
  background: #f5f7fa;
  border-radius: 4px;
  padding: 12px;
  font-size: 12px;
  overflow: auto;
}
</style>
