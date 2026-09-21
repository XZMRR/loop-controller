<template>
  <div>
    <el-card shadow="hover">
      <template #header>
        <span>审计查询</span>
      </template>
      <el-form :model="filters" inline>
        <el-form-item label="Session ID">
          <el-input v-model="filters.session_id" placeholder="可选" clearable />
        </el-form-item>
        <el-form-item label="Task ID">
          <el-input v-model="filters.task_id" placeholder="可选" clearable />
        </el-form-item>
        <el-form-item label="Agent ID">
          <el-input v-model="filters.agent_id" placeholder="可选" clearable />
        </el-form-item>
        <el-form-item label="工具">
          <el-input v-model="filters.tool_name" placeholder="可选" clearable />
        </el-form-item>
        <el-form-item label="时间范围">
          <el-date-picker
            v-model="timeRange"
            type="datetimerange"
            range-separator="至"
            start-placeholder="开始时间"
            end-placeholder="结束时间"
            clearable
            style="width: 340px"
          />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="loadAudit" :loading="loading">查询</el-button>
        </el-form-item>
      </el-form>
      <ErrorState
        v-if="!loading && loadError"
        :message="loadError"
        @retry="loadAudit"
      />
      <el-table :data="events" v-loading="loading" stripe>
        <el-table-column prop="timestamp" label="时间" width="180" />
        <el-table-column prop="action" label="动作" width="120" />
        <el-table-column prop="agent_id" label="Agent" width="140" />
        <el-table-column prop="tool_name" label="工具" width="140" />
        <el-table-column prop="verdict" label="结果" width="100" />
        <el-table-column prop="reason" label="原因" show-overflow-tooltip />
        <template #empty>
          <el-empty description="暂无审计事件" />
        </template>
      </el-table>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { getAuditEvents } from '@/api/python'
import ErrorState from '@/components/ErrorState.vue'
import type { AuditEvent } from '@/api/python'

const filters = reactive({
  session_id: '',
  task_id: '',
  agent_id: '',
  tool_name: '',
  limit: 100,
})

const timeRange = ref<[Date, Date] | null>(null)

const events = ref<AuditEvent[]>([])
const loading = ref(false)
// 加载失败的持久错误态（由 ErrorState 展示，含重试）
const loadError = ref('')

async function loadAudit() {
  loading.value = true
  try {
    const params = {
      ...Object.fromEntries(Object.entries(filters).filter(([, v]) => v !== '')),
      start_time: timeRange.value?.[0].toISOString(),
      end_time: timeRange.value?.[1].toISOString(),
    }
    events.value = await getAuditEvents(params)
    loadError.value = ''
  } catch (error: any) {
    loadError.value = error.message || '加载审计失败'
  } finally {
    loading.value = false
  }
}

onMounted(loadAudit)
</script>
