<template>
  <div>
    <el-card shadow="hover">
      <template #header>
        <div class="card-header">
          <span>审批中心</span>
          <div>
            <el-radio-group v-model="activeTab" style="margin-right: 12px">
              <el-radio-button value="pending">待审批</el-radio-button>
              <el-radio-button value="history">历史</el-radio-button>
              <el-radio-button value="kernel">内核对账</el-radio-button>
            </el-radio-group>
            <el-button type="primary" :icon="Refresh" @click="handleRefresh" :loading="loading">
              刷新
            </el-button>
          </div>
        </div>
      </template>

      <template v-if="activeTab === 'pending'">
        <el-table :data="approvals" v-loading="loading" stripe>
          <el-table-column prop="decision_id" label="Decision ID" width="220" show-overflow-tooltip />
          <el-table-column prop="tool_name" label="工具" width="160" />
          <el-table-column prop="requester_id" label="申请人" width="140" />
          <el-table-column prop="reason" label="原因" show-overflow-tooltip />
          <el-table-column label="操作" width="220" fixed="right">
            <template #default="{ row }">
              <el-button type="success" size="small" @click="openAction(row, 'approve')">
                通过
              </el-button>
              <el-button type="danger" size="small" @click="openAction(row, 'deny')">
                拒绝
              </el-button>
            </template>
          </el-table-column>
        </el-table>
        <el-empty v-if="!loading && approvals.length === 0" description="暂无待审批事项" />
      </template>

      <template v-else>
        <el-form inline style="margin-bottom: 16px">
          <el-form-item label="状态">
            <el-select v-model="historyQuery.status" placeholder="全部" clearable style="width: 140px">
              <el-option label="待审批" value="pending" />
              <el-option label="通过" value="approve" />
              <el-option label="拒绝" value="deny" />
            </el-select>
          </el-form-item>
          <el-form-item label="工具">
            <el-input v-model="historyQuery.tool_name" placeholder="send_email" clearable style="width: 180px" />
          </el-form-item>
          <el-form-item label="Agent">
            <el-input v-model="historyQuery.agent_id" placeholder="researcher_001" clearable style="width: 180px" />
          </el-form-item>
          <el-form-item>
            <el-button type="primary" @click="loadHistory">查询</el-button>
          </el-form-item>
        </el-form>

        <el-table :data="history" v-loading="loading" stripe>
          <el-table-column prop="created_at" label="发起时间" width="180" />
          <el-table-column prop="decision_id" label="Decision ID" width="220" show-overflow-tooltip />
          <el-table-column prop="agent_id" label="Agent" width="140" />
          <el-table-column prop="tool_name" label="工具" width="150" />
          <el-table-column prop="requester_id" label="申请人" width="120" />
          <el-table-column prop="approver_id" label="审批人" width="120" />
          <el-table-column prop="status" label="状态" width="100">
            <template #default="{ row }">
              <el-tag :type="statusTagType(row.status)">{{ statusLabel(row.status) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="reason" label="原因" show-overflow-tooltip />
        </el-table>
        <div style="display: flex; justify-content: flex-end; margin-top: 16px">
          <el-pagination
            background
            layout="prev, pager, next"
            :total="historyTotal"
            :page-size="historyQuery.limit"
            :current-page="historyPage"
            @current-change="handleHistoryPageChange"
          />
        </div>
      </template>

      <template v-if="activeTab === 'kernel'">
        <el-alert
          v-if="!kernelEnabled"
          type="info"
          :closable="false"
          title="Go 内核未启用，暂无内核审批单可对账"
          style="margin-bottom: 16px"
        />
        <template v-else>
          <el-form inline style="margin-bottom: 16px">
            <el-form-item label="内核状态">
              <el-select v-model="kernelStatus" placeholder="全部" clearable style="width: 140px" @change="loadKernelApprovals">
                <el-option label="待审批" value="pending" />
                <el-option label="已批准" value="approved" />
                <el-option label="已消费" value="consumed" />
                <el-option label="已拒绝" value="rejected" />
                <el-option label="已过期" value="expired" />
                <el-option label="已取消" value="cancelled" />
              </el-select>
            </el-form-item>
          </el-form>
          <el-table :data="kernelApprovals" v-loading="kernelLoading" stripe>
            <el-table-column prop="kernel.approval_id" label="内核 Approval ID" width="220" show-overflow-tooltip />
            <el-table-column prop="kernel.target_agent_id" label="目标 Agent" width="140" />
            <el-table-column label="工具" width="150">
              <template #default="{ row }">
                {{ (row.kernel.allowed_tools || [])[0] || '-' }}
              </template>
            </el-table-column>
            <el-table-column label="内核状态" width="110">
              <template #default="{ row }">
                <el-tag :type="kernelStatusTagType(row.kernel.status)">{{ kernelStatusLabel(row.kernel.status) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="任务" width="200" show-overflow-tooltip>
              <template #default="{ row }">
                {{ row.kernel.task_id || '-' }}
              </template>
            </el-table-column>
            <el-table-column label="审批台关联" width="130">
              <template #default="{ row }">
                <el-tag v-if="row.reconciled" type="success">已关联</el-tag>
                <el-tag v-else type="warning">未关联</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="审批台结论" width="110">
              <template #default="{ row }">
                {{ row.console_verdict ? statusLabel(row.console_verdict) : '-' }}
              </template>
            </el-table-column>
            <el-table-column prop="kernel.expires_at" label="过期时间" width="180" />
          </el-table>
          <el-empty v-if="!kernelLoading && kernelApprovals.length === 0" description="暂无内核对账记录" />
        </template>
      </template>
    </el-card>

    <el-dialog v-model="dialogVisible" :title="dialogTitle" width="500px">
      <el-form :model="form" label-position="top">
        <el-form-item label="审批人">
          <el-input v-model="form.approver" placeholder="请输入审批人 ID" />
        </el-form-item>
        <el-form-item label="审批意见">
          <el-input
            v-model="form.comment"
            type="textarea"
            :rows="3"
            placeholder="请输入审批意见（拒绝时必须填写）"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" @click="submitAction" :loading="submitting">
          确认
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import {
  getPendingApprovals,
  getApprovalHistory,
  approveDecision,
  denyDecision,
  getKernelApprovals,
} from '@/api/python'
import type { PendingApproval, ApprovalHistoryItem, KernelApprovalReconciliationItem } from '@/api/python'

const activeTab = ref<'pending' | 'history' | 'kernel'>('pending')
const approvals = ref<PendingApproval[]>([])
const history = ref<ApprovalHistoryItem[]>([])
const historyTotal = ref(0)
const historyPage = ref(1)
const historyQuery = ref({
  status: '',
  tool_name: '',
  agent_id: '',
  limit: 10,
  offset: 0,
})
const loading = ref(false)
const dialogVisible = ref(false)
const submitting = ref(false)
const currentRow = ref<PendingApproval | null>(null)
const currentAction = ref<'approve' | 'deny'>('approve')
const form = ref({ approver: '', comment: '' })
const kernelEnabled = ref(false)
const kernelApprovals = ref<KernelApprovalReconciliationItem[]>([])
const kernelStatus = ref('')
const kernelLoading = ref(false)

const dialogTitle = computed(() => (currentAction.value === 'approve' ? '通过审批' : '拒绝审批'))

function statusLabel(status: string) {
  if (status === 'pending') return '待审批'
  if (status === 'approve') return '通过'
  if (status === 'deny') return '拒绝'
  return status
}

function statusTagType(status: string) {
  if (status === 'pending') return 'warning'
  if (status === 'approve') return 'success'
  if (status === 'deny') return 'danger'
  return 'info'
}

async function loadApprovals() {
  loading.value = true
  try {
    approvals.value = await getPendingApprovals()
  } catch (error: any) {
    ElMessage.error(error.message || '加载审批失败')
  } finally {
    loading.value = false
  }
}

async function loadHistory() {
  loading.value = true
  try {
    const data = await getApprovalHistory({
      status: historyQuery.value.status || undefined,
      tool_name: historyQuery.value.tool_name || undefined,
      agent_id: historyQuery.value.agent_id || undefined,
      limit: historyQuery.value.limit,
      offset: historyQuery.value.offset,
    })
    history.value = data.approvals
    historyTotal.value = data.total
  } catch (error: any) {
    ElMessage.error(error.message || '加载审批历史失败')
  } finally {
    loading.value = false
  }
}

function handleRefresh() {
  if (activeTab.value === 'pending') {
    loadApprovals()
  } else if (activeTab.value === 'history') {
    loadHistory()
  } else {
    loadKernelApprovals()
  }
}

function kernelStatusLabel(status: string) {
  const map: Record<string, string> = {
    pending: '待审批',
    approved: '已批准',
    consumed: '已消费',
    rejected: '已拒绝',
    expired: '已过期',
    cancelled: '已取消',
  }
  return map[status] || status
}

function kernelStatusTagType(status: string) {
  const map: Record<string, string> = {
    pending: 'warning',
    approved: 'primary',
    consumed: 'success',
    rejected: 'danger',
    expired: 'info',
    cancelled: 'info',
  }
  return map[status] || 'info'
}

async function loadKernelApprovals() {
  kernelLoading.value = true
  try {
    const data = await getKernelApprovals(kernelStatus.value || undefined)
    kernelEnabled.value = data.enabled
    kernelApprovals.value = data.approvals
  } catch (error: any) {
    ElMessage.error(error.message || '加载内核对账失败')
  } finally {
    kernelLoading.value = false
  }
}

function handleHistoryPageChange(page: number) {
  historyPage.value = page
  historyQuery.value.offset = (page - 1) * historyQuery.value.limit
  loadHistory()
}

function openAction(row: PendingApproval, action: 'approve' | 'deny') {
  currentRow.value = row
  currentAction.value = action
  form.value = { approver: '', comment: '' }
  dialogVisible.value = true
}

async function submitAction() {
  if (!currentRow.value) return
  if (!form.value.approver.trim()) {
    ElMessage.warning('请输入审批人')
    return
  }
  if (currentAction.value === 'deny' && !form.value.comment.trim()) {
    ElMessage.warning('拒绝时必须填写审批意见')
    return
  }
  submitting.value = true
  try {
    const body = { approver: form.value.approver, comment: form.value.comment }
    if (currentAction.value === 'approve') {
      await approveDecision(currentRow.value.decision_id, body)
      ElMessage.success('已通过')
    } else {
      await denyDecision(currentRow.value.decision_id, body)
      ElMessage.success('已拒绝')
    }
    dialogVisible.value = false
    await Promise.all([loadApprovals(), loadHistory()])
  } catch (error: any) {
    ElMessage.error(error.message || '操作失败')
  } finally {
    submitting.value = false
  }
}

onMounted(() => {
  loadApprovals()
  loadHistory()
  loadKernelApprovals()
})
</script>

<style scoped>
.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
</style>
