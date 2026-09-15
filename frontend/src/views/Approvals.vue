<template>
  <div>
    <el-card shadow="hover">
      <template #header>
        <div class="card-header">
          <span>待审批列表</span>
          <el-button type="primary" :icon="Refresh" @click="loadApprovals" :loading="loading">
            刷新
          </el-button>
        </div>
      </template>
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
import { getPendingApprovals, approveDecision, denyDecision } from '@/api/python'
import type { PendingApproval } from '@/api/python'

const approvals = ref<PendingApproval[]>([])
const loading = ref(false)
const dialogVisible = ref(false)
const submitting = ref(false)
const currentRow = ref<PendingApproval | null>(null)
const currentAction = ref<'approve' | 'deny'>('approve')
const form = ref({ approver: '', comment: '' })

const dialogTitle = computed(() => (currentAction.value === 'approve' ? '通过审批' : '拒绝审批'))

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
    await loadApprovals()
  } catch (error: any) {
    ElMessage.error(error.message || '操作失败')
  } finally {
    submitting.value = false
  }
}

onMounted(loadApprovals)
</script>

<style scoped>
.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
</style>
