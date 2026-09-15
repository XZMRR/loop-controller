<template>
  <div>
    <el-card shadow="hover">
      <template #header>
        <div class="card-header">
          <span>工具策略</span>
          <div>
            <el-button size="small" :loading="reloading" @click="handleReload">
              从磁盘重载
            </el-button>
            <el-button size="small" @click="loadData">刷新</el-button>
          </div>
        </div>
      </template>
      <el-alert
        type="info"
        :closable="false"
        class="mb-4"
        title="保存后会写回 profiles.yaml 并立即热更新到运行时；文件中的注释在首次写回后不保留。"
      />
      <el-collapse v-model="activeProfiles">
        <el-collapse-item
          v-for="profile in profiles"
          :key="profile.profile_id"
          :title="`${profile.profile_id} - ${profile.description || '无描述'}`"
          :name="profile.profile_id"
        >
          <div class="profile-actions">
            <el-descriptions :column="3" border>
              <el-descriptions-item label="Token 预算">{{ profile.max_budget_token }}</el-descriptions-item>
              <el-descriptions-item label="支付预算">{{ profile.max_budget_payment }}</el-descriptions-item>
              <el-descriptions-item label="工具数量">{{ Object.keys(profile.tools || {}).length }}</el-descriptions-item>
            </el-descriptions>
            <el-button size="small" type="primary" @click="openEditor(profile)">编辑</el-button>
          </div>
          <el-table :data="formatTools(profile.tools)" stripe class="tool-table">
            <el-table-column prop="name" label="工具名" width="180" />
            <el-table-column label="允许" width="100">
              <template #default="{ row }">
                <el-tag :type="row.allowed ? 'success' : 'danger'">
                  {{ row.allowed ? '是' : '否' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="需审批" width="100">
              <template #default="{ row }">
                <el-tag :type="row.require_approval ? 'warning' : 'info'">
                  {{ row.require_approval ? '是' : '否' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="max_calls_per_task" label="每任务上限" width="120" />
            <el-table-column prop="allowed_args" label="参数白名单" show-overflow-tooltip />
          </el-table>
        </el-collapse-item>
      </el-collapse>
    </el-card>

    <el-dialog
      v-model="editVisible"
      :title="`编辑工具策略 - ${editProfileId}`"
      width="860px"
      :close-on-click-modal="false"
    >
      <el-table :data="editRows" stripe size="small">
        <el-table-column prop="name" label="工具名" width="160" />
        <el-table-column label="允许" width="80" align="center">
          <template #default="{ row }">
            <el-switch v-model="row.allowed" />
          </template>
        </el-table-column>
        <el-table-column label="需审批" width="80" align="center">
          <template #default="{ row }">
            <el-switch v-model="row.require_approval" :disabled="!row.allowed" />
          </template>
        </el-table-column>
        <el-table-column label="每任务上限" width="150">
          <template #default="{ row }">
            <el-input-number v-model="row.max_calls_per_task" :min="1" :max="9999" size="small" />
          </template>
        </el-table-column>
        <el-table-column label="参数规则" min-width="160">
          <template #default="{ row }">
            <el-button size="small" link type="primary" @click="openArgsEditor(row)">
              {{ argsSummary(row) }}
            </el-button>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="70" align="center">
          <template #default="{ $index }">
            <el-button size="small" link type="danger" @click="editRows.splice($index, 1)">
              删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>
      <div class="add-tool">
        <el-input v-model="newToolName" placeholder="新工具名" size="small" style="width: 200px" />
        <el-button size="small" @click="addTool" :disabled="!newToolName.trim()">添加工具</el-button>
      </div>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="saveEdit">保存并热更新</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="argsVisible" :title="`参数规则 - ${argsRow?.name || ''}`" width="560px">
      <el-form label-position="top" size="small">
        <el-form-item label="allowed_args（参数白名单，JSON：{参数: [glob]}）">
          <el-input v-model="argsAllowedJson" type="textarea" :rows="4" />
        </el-form-item>
        <el-form-item label="denied_args（参数黑名单，JSON）">
          <el-input v-model="argsDeniedJson" type="textarea" :rows="4" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="argsVisible = false">取消</el-button>
        <el-button type="primary" @click="saveArgs">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { loadProfilesConfig } from '@/api/config'
import {
  getAdminProfiles,
  updateProfileTools,
  reloadProfiles,
  type ToolPermissionInput,
} from '@/api/python'

const profiles = ref<any[]>([])
const activeProfiles = ref<string[]>([])
const reloading = ref(false)

const editVisible = ref(false)
const editProfileId = ref('')
const editRows = ref<any[]>([])
const newToolName = ref('')
const saving = ref(false)

const argsVisible = ref(false)
const argsRow = ref<any | null>(null)
const argsAllowedJson = ref('{}')
const argsDeniedJson = ref('{}')

function formatTools(tools: Record<string, any>) {
  return Object.entries(tools || {}).map(([name, policy]) => ({
    name,
    ...policy,
    allowed_args: policy.allowed_args ? JSON.stringify(policy.allowed_args) : '-',
  }))
}

function argsSummary(row: any) {
  const parts: string[] = []
  const allowCount = Object.keys(row.allowed_args || {}).length
  const denyCount = Object.keys(row.denied_args || {}).length
  if (allowCount) parts.push(`白名单 ${allowCount}`)
  if (denyCount) parts.push(`黑名单 ${denyCount}`)
  return parts.length ? parts.join(' / ') : '无限制'
}

async function loadData() {
  try {
    // 优先在线 API（含热更新后的最新版本），失败回退静态 YAML
    try {
      profiles.value = await getAdminProfiles()
    } catch {
      const config = await loadProfilesConfig()
      profiles.value = config.profiles || []
    }
    activeProfiles.value = profiles.value.map((p: any) => p.profile_id)
  } catch (error: any) {
    ElMessage.error(error.message || '加载工具策略失败')
  }
}

function openEditor(profile: any) {
  editProfileId.value = profile.profile_id
  editRows.value = Object.entries(profile.tools || {}).map(([name, policy]: [string, any]) => ({
    name,
    allowed: policy.allowed ?? false,
    require_approval: policy.require_approval ?? false,
    max_calls_per_task: policy.max_calls_per_task ?? null,
    allowed_args: policy.allowed_args || {},
    denied_args: policy.denied_args || {},
  }))
  newToolName.value = ''
  editVisible.value = true
}

function addTool() {
  const name = newToolName.value.trim()
  if (!name || editRows.value.some((row) => row.name === name)) return
  editRows.value.push({
    name,
    allowed: false,
    require_approval: false,
    max_calls_per_task: null,
    allowed_args: {},
    denied_args: {},
  })
  newToolName.value = ''
}

function openArgsEditor(row: any) {
  argsRow.value = row
  argsAllowedJson.value = JSON.stringify(row.allowed_args || {}, null, 2)
  argsDeniedJson.value = JSON.stringify(row.denied_args || {}, null, 2)
  argsVisible.value = true
}

function saveArgs() {
  if (!argsRow.value) return
  try {
    argsRow.value.allowed_args = JSON.parse(argsAllowedJson.value || '{}')
    argsRow.value.denied_args = JSON.parse(argsDeniedJson.value || '{}')
    argsVisible.value = false
  } catch {
    ElMessage.error('JSON 格式错误，请检查参数规则')
  }
}

async function saveEdit() {
  const tools: Record<string, ToolPermissionInput> = {}
  for (const row of editRows.value) {
    tools[row.name] = {
      allowed: row.allowed,
      require_approval: row.require_approval,
      allowed_args: row.allowed_args,
      denied_args: row.denied_args,
      max_calls_per_task: row.max_calls_per_task,
    }
  }
  saving.value = true
  try {
    await updateProfileTools(editProfileId.value, tools)
    ElMessage.success('已保存并热更新到运行时')
    editVisible.value = false
    await loadData()
  } catch (error: any) {
    const message = error?.response?.data?.message || error.message || '保存失败'
    ElMessage.error(message)
  } finally {
    saving.value = false
  }
}

async function handleReload() {
  reloading.value = true
  try {
    profiles.value = await reloadProfiles()
    activeProfiles.value = profiles.value.map((p: any) => p.profile_id)
    ElMessage.success('已从磁盘重载全部 Profile')
  } catch (error: any) {
    ElMessage.error(error.message || '重载失败（需要后端在线）')
  } finally {
    reloading.value = false
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

.mb-4 {
  margin-bottom: 16px;
}

.profile-actions {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
}

.tool-table {
  margin-top: 16px;
}

.add-tool {
  display: flex;
  gap: 8px;
  margin-top: 12px;
}
</style>
