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
        <span>发起委托（管理端入口，与 Agent 自发委托同治理路径）</span>
      </template>
      <el-form label-position="top">
        <el-row :gutter="16">
          <el-col :span="6">
            <el-form-item label="发起 Agent">
              <el-select v-model="delegation.source_agent_id" placeholder="选择发起方" style="width: 100%">
                <el-option v-for="a in agents" :key="a.agent_id" :label="a.agent_id" :value="a.agent_id" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="6">
            <el-form-item label="目标 Agent">
              <el-select v-model="delegation.target_agent_id" placeholder="选择目标方" style="width: 100%">
                <el-option v-for="a in agents" :key="a.agent_id" :label="a.agent_id" :value="a.agent_id" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="6">
            <el-form-item label="能力 / 工具">
              <el-input v-model="delegation.tool_name" placeholder="如 send_email" />
            </el-form-item>
          </el-col>
          <el-col :span="6">
            <el-form-item label="风险等级">
              <el-select v-model="delegation.risk_level" style="width: 100%">
                <el-option label="low" value="low" />
                <el-option label="medium" value="medium" />
                <el-option label="high" value="high" />
                <el-option label="critical" value="critical" />
              </el-select>
            </el-form-item>
          </el-col>
        </el-row>
        <el-form-item label="参数（JSON，可留空）">
          <el-input v-model="delegation.argumentsJson" type="textarea" :rows="3" placeholder='{"to": "manager@company.com"}' />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="delegating" :disabled="!canSubmit" @click="submitDelegation">
            发起委托
          </el-button>
        </el-form-item>
      </el-form>
      <template v-if="delegationResult">
        <el-divider />
        <el-descriptions :column="2" border>
          <el-descriptions-item label="判定">
            <el-tag :type="verdictTagType(delegationResult.verdict)">{{ delegationResult.verdict }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="原因">{{ delegationResult.reason }}</el-descriptions-item>
          <el-descriptions-item label="Decision ID">{{ delegationResult.decision_id }}</el-descriptions-item>
          <el-descriptions-item label="Interaction ID">{{ delegationResult.interaction_id }}</el-descriptions-item>
          <el-descriptions-item v-if="delegationResult.escalation_target" label="升级对象">
            {{ delegationResult.escalation_target }}
          </el-descriptions-item>
          <el-descriptions-item label="派发">
            <template v-if="delegationResult.dispatch.attempted">
              {{ delegationResult.dispatch.accepted ? `已接受（任务 ${delegationResult.dispatch.task_id}）` : `被拒绝：${delegationResult.dispatch.reason}` }}
            </template>
            <template v-else>{{ delegationResult.dispatch.reason || '未派发（判定未通过）' }}</template>
          </el-descriptions-item>
        </el-descriptions>
        <el-alert
          v-if="delegationResult.verdict === 'require_approval'"
          class="mt-4"
          type="warning"
          :closable="false"
        >
          <template #title>
            <span v-if="delegationResult.approval">
              该委托需要审批：已提交审批单（审批人 {{ delegationResult.approval.approver_id }}），
              批准后将自动派发。
              <router-link to="/approvals" style="color: inherit; text-decoration: underline">
                前往审批台处理
              </router-link>
            </span>
            <span v-else>该委托需要审批：请联系升级对象确认后重新发起。</span>
          </template>
        </el-alert>
      </template>
    </el-card>

    <el-card class="mt-4" shadow="hover">
      <template #header>
        <div class="card-header">
          <span>任务查询</span>
          <el-tag v-if="streaming" type="primary" effect="dark">流式订阅中</el-tag>
        </div>
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
        <div class="task-actions">
          <el-tag :type="taskStatusTagType(task.status)" effect="dark">{{ task.status }}</el-tag>
          <el-button
            v-if="!isTerminalStatus(task.status)"
            type="danger"
            size="small"
            :loading="canceling"
            @click="cancelTask"
          >
            取消任务
          </el-button>
          <el-button
            v-if="!streaming"
            size="small"
            :disabled="isTerminalStatus(task.status)"
            @click="startStream"
          >
            订阅状态流
          </el-button>
          <el-button v-else size="small" @click="stopStream">停止订阅</el-button>
        </div>
        <pre class="task-result">{{ JSON.stringify(task, null, 2) }}</pre>
      </template>
      <el-empty v-else-if="taskQueried" description="任务不存在或内核不可达" />
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, reactive, onMounted, onBeforeUnmount } from 'vue'
import { ElMessage } from 'element-plus'
import {
  getA2AStatus,
  getA2AAgents,
  getA2ATask,
  cancelA2ATask,
  streamA2ATask,
  createAdminDelegation,
  type A2AStatus,
  type A2AAgent,
  type AdminDelegationResult,
} from '@/api/python'

const status = ref<A2AStatus | null>(null)
const agents = ref<A2AAgent[]>([])
const loading = ref(false)

const taskId = ref('')
const task = ref<Record<string, any> | null>(null)
const taskQueried = ref(false)
const querying = ref(false)
const canceling = ref(false)
const streaming = ref(false)
let stopStreamFn: (() => void) | null = null

const TERMINAL_STATUSES = ['completed', 'failed', 'canceled', 'cancelled', 'rejected']

function isTerminalStatus(status: unknown) {
  return TERMINAL_STATUSES.includes(String(status ?? ''))
}

function taskStatusTagType(status: unknown) {
  const s = String(status ?? '')
  if (s === 'completed') return 'success'
  if (s === 'failed' || s === 'rejected') return 'danger'
  if (s === 'canceled' || s === 'cancelled') return 'info'
  return 'primary'
}

const delegation = reactive({
  source_agent_id: '',
  target_agent_id: '',
  tool_name: '',
  risk_level: 'low',
  argumentsJson: '',
})
const delegating = ref(false)
const delegationResult = ref<AdminDelegationResult | null>(null)

const canSubmit = computed(
  () =>
    delegation.source_agent_id &&
    delegation.target_agent_id &&
    delegation.tool_name.trim() &&
    !delegating.value,
)

function verdictTagType(verdict: string) {
  if (verdict === 'allow' || verdict === 'modify') return 'success'
  if (verdict === 'require_approval') return 'warning'
  return 'danger'
}

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

async function submitDelegation() {
  let args: Record<string, any> = {}
  if (delegation.argumentsJson.trim()) {
    try {
      args = JSON.parse(delegation.argumentsJson)
    } catch {
      ElMessage.error('参数 JSON 格式错误')
      return
    }
  }
  delegating.value = true
  delegationResult.value = null
  try {
    delegationResult.value = await createAdminDelegation({
      source_agent_id: delegation.source_agent_id,
      target_agent_id: delegation.target_agent_id,
      tool_name: delegation.tool_name.trim(),
      arguments: args,
      risk_level: delegation.risk_level,
    })
    if (!delegationResult.value.allowed) {
      ElMessage.warning(`委托未通过治理判定：${delegationResult.value.reason}`)
    }
  } catch (error: any) {
    const message = error?.response?.data?.message || error.message || '发起委托失败'
    ElMessage.error(message)
  } finally {
    delegating.value = false
  }
}

async function queryTask() {
  querying.value = true
  taskQueried.value = false
  stopStream()
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

async function cancelTask() {
  canceling.value = true
  try {
    const updated = await cancelA2ATask(taskId.value.trim(), 'operator cancel from admin console')
    task.value = updated
    ElMessage.success(`任务已取消：${updated.status}`)
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.error || error.message || '取消任务失败')
  } finally {
    canceling.value = false
  }
}

function startStream() {
  const id = taskId.value.trim()
  if (!id) return
  stopStreamFn = streamA2ATask(
    id,
    (event) => {
      // 内核事件即任务快照：就地更新并展示
      task.value = event
      taskQueried.value = true
      if (isTerminalStatus(event?.status)) {
        streaming.value = false
        stopStreamFn = null
      }
    },
    (error) => {
      streaming.value = false
      stopStreamFn = null
      ElMessage.error(`状态流中断：${error.message}`)
    },
  )
  streaming.value = true
}

function stopStream() {
  stopStreamFn?.()
  stopStreamFn = null
  streaming.value = false
}

onBeforeUnmount(stopStream)

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

.task-actions {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
}
</style>
