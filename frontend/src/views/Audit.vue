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
        <el-form-item>
          <el-button type="primary" @click="loadAudit" :loading="loading">查询</el-button>
        </el-form-item>
      </el-form>
      <el-table :data="events" v-loading="loading" stripe>
        <el-table-column prop="timestamp" label="时间" width="180" />
        <el-table-column prop="action" label="动作" width="120" />
        <el-table-column prop="agent_id" label="Agent" width="140" />
        <el-table-column prop="tool_name" label="工具" width="140" />
        <el-table-column prop="verdict" label="结果" width="100" />
        <el-table-column prop="reason" label="原因" show-overflow-tooltip />
      </el-table>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { getAuditEvents } from '@/api/python'
import type { AuditEvent } from '@/api/python'

const filters = reactive({
  session_id: '',
  task_id: '',
  agent_id: '',
  tool_name: '',
  limit: 100,
})

const events = ref<AuditEvent[]>([])
const loading = ref(false)

async function loadAudit() {
  loading.value = true
  try {
    const params = Object.fromEntries(
      Object.entries(filters).filter(([, v]) => v !== '')
    )
    events.value = await getAuditEvents(params)
  } catch (error: any) {
    ElMessage.error(error.message || '加载审计失败')
  } finally {
    loading.value = false
  }
}

onMounted(loadAudit)
</script>
