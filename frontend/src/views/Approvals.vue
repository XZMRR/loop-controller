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
    </el-card>

    <el-dialog v-model="dialogVisible" :title="dialogTitle" width="500px" @closed="clearForm">
      <el-form :model="form" label-position="top">
        <el-form-item label="独立审批凭证">
          <el-input v-model="form.credential" type="password" show-password autocomplete="off" placeholder="仅用于本次审批，不会保存" />
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
} from '@/api/python'
import type { PendingApproval, ApprovalHistoryItem } from '@/api/python'

const activeTab = ref<'pending' | 'history'>('pending')
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
const form = ref({ credential: '', comment: '' })

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
  } else {
    loadHistory()
  }
}

function handleHistoryPageChange(page: number) {
  historyPage.value = page
  historyQuery.value.offset = (page - 1) * historyQuery.value.limit
  loadHistory()
}

function clearForm() {
  form.value = { credential: '', comment: '' }
}

function openAction(row: PendingApproval, action: 'approve' | 'deny') {
  currentRow.value = row
  currentAction.value = action
  clearForm()
  dialogVisible.value = true
}

async function submitAction() {
  if (!currentRow.value || submitting.value) return
  if (!form.value.credential.trim()) {
    ElMessage.warning('请输入独立审批凭证')
    return
  }
  if (currentAction.value === 'deny' && !form.value.comment.trim()) {
    ElMessage.warning('拒绝时必须填写审批意见')
    return
  }
  submitting.value = true
  const credential = form.value.credential.trim()
  form.value.credential = ''
  try {
    const body = { comment: form.value.comment }
    if (currentAction.value === 'approve') {
      await approveDecision(currentRow.value.decision_id, body, credential)
      ElMessage.success('已通过')
    } else {
      await denyDecision(currentRow.value.decision_id, body, credential)
      ElMessage.success('已拒绝')
    }
    dialogVisible.value = false
    await Promise.all([loadApprovals(), loadHistory()])
  } catch {
    ElMessage.error('审批失败，请检查独立审批凭证及审批权限后重试')
  } finally {
    submitting.value = false
  }
}

onMounted(() => {
  loadApprovals()
  loadHistory()
})
</script>

<style scoped>
.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
</style>
