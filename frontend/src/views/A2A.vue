<template>
  <div>
    <el-card shadow="hover">
      <template #header>
        <div class="card-header">
          <span>Go A2A Kernel 状态</span>
          <el-button size="small" :loading="loading" @click="loadData">刷新</el-button>
        </div>
      </template>
      <el-descriptions :column="2" border>
        <el-descriptions-item label="启用状态">
          <el-tag :type="status?.enabled ? 'success' : 'info'">
            {{ status?.enabled ? '已启用' : '未启用' }}
          </el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="内核可达">
          <el-tag :type="status?.reachable ? 'success' : 'danger'">
            {{ status?.reachable ? '可达' : '不可达' }}
          </el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="内核地址">{{ status?.base_url || '-' }}</el-descriptions-item>
        <el-descriptions-item label="本地 Agent">
          {{ status?.local_agent?.agent_id || '-' }}
        </el-descriptions-item>
      </el-descriptions>
      <el-alert
        v-if="status && !status.enabled"
        class="mt-4"
        type="warning"
        :closable="false"
        title="go_kernel.yaml 中 enabled=false：请在 config/go_kernel.yaml 启用并启动 Go 内核后重试。"
      />
      <el-alert
        v-else-if="status && status.enabled && !status.reachable"
        class="mt-4"
        type="error"
        :closable="false"
        title="Go 内核已启用但不可达：请检查 Go 内核进程与 base_url 配置。"
      />
    </el-card>

    <el-card class="mt-4" shadow="hover">
      <template #header>
        <span>Agent 注册状态</span>
      </template>
      <el-table :data="agents" stripe>
        <el-table-column prop="agent_id" label="Agent ID" width="200" />
        <el-table-column prop="name" label="名称" width="200" />
        <el-table-column prop="profile_id" label="Profile" width="180" />
        <el-table-column prop="owner_name" label="Owner" />
        <el-table-column label="内核注册" width="120">
          <template #default="{ row }">
            <el-tag v-if="row.registered === true" type="success">已注册</el-tag>
            <el-tag v-else-if="row.registered === false" type="warning">未注册</el-tag>
            <el-tag v-else type="info">未知</el-tag>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-card class="mt-4" shadow="hover">
      <template #header>
        <span>任务查询</span>
      </template>
      <el-form inline @submit.prevent="queryTask">
        <el-form-item>
          <el-input v-model="taskId" placeholder="Task ID" style="width: 320px" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" native-type="submit" :loading="querying" :disabled="!taskId.trim()">
            查询
          </el-button>
        </el-form-item>
      </el-form>
      <template v-if="task">
        <pre class="task-result">{{ JSON.stringify(task, null, 2) }}</pre>
      </template>
      <el-empty v-else-if="taskQueried" description="任务不存在或内核不可达" />
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { getA2AStatus, getA2AAgents, getA2ATask, type A2AStatus, type A2AAgent } from '@/api/python'

const status = ref<A2AStatus | null>(null)
const agents = ref<A2AAgent[]>([])
const loading = ref(false)

const taskId = ref('')
const task = ref<Record<string, any> | null>(null)
const taskQueried = ref(false)
const querying = ref(false)

async function loadData() {
  loading.value = true
  try {
    status.value = await getA2AStatus()
    const data = await getA2AAgents()
    agents.value = data.agents
  } catch (error: any) {
    ElMessage.error(error.message || '加载 A2A 状态失败')
  } finally {
    loading.value = false
  }
}

async function queryTask() {
  querying.value = true
  taskQueried.value = false
  task.value = null
  try {
    task.value = await getA2ATask(taskId.value.trim())
  } catch {
    // 404/503：展示空状态
  } finally {
    taskQueried.value = true
    querying.value = false
  }
}

onMounted(loadData)
</script>

<style scoped>
.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.mt-4 {
  margin-top: 16px;
}

.task-result {
  background: #f5f7fa;
  border-radius: 4px;
  padding: 12px;
  font-size: 12px;
  overflow: auto;
}
</style>
