<template>
  <div>
    <el-card>
      <el-tabs v-model="activeTab">
        <!-- ============ 角色绑定 ============ -->
        <el-tab-pane label="角色绑定" name="bindings">
          <div class="toolbar">
            <div class="toolbar-left">
              <span class="label">租户</span>
              <el-select
                v-model="bindingTenantFilter"
                placeholder="全部租户"
                clearable
                style="width: 180px"
              >
                <el-option
                  v-for="tenant in bindingTenantOptions"
                  :key="tenant"
                  :label="tenant"
                  :value="tenant"
                />
              </el-select>
              <span class="label">角色</span>
              <el-select
                v-model="bindingRoleFilter"
                placeholder="全部角色"
                clearable
                style="width: 180px"
              >
                <el-option
                  v-for="role in ROLE_OPTIONS"
                  :key="role"
                  :label="role"
                  :value="role"
                />
              </el-select>
              <el-tag v-if="bindings" type="info" effect="plain">
                共 {{ filteredBindings.length }} 条绑定
              </el-tag>
            </div>
            <div>
              <el-button :icon="Refresh" @click="refreshBindings()" :loading="bindingsLoading">
                刷新
              </el-button>
              <el-button type="primary" :icon="Plus" @click="openBindingDialog()">
                新建绑定
              </el-button>
            </div>
          </div>

          <ErrorState
            v-if="!bindingsLoading && bindingsError"
            :message="bindingsError"
            @retry="refreshBindings()"
          />
          <el-table v-loading="bindingsLoading" :data="filteredBindings" style="width: 100%">
            <el-table-column prop="binding_id" label="Binding ID" min-width="130" show-overflow-tooltip />
            <el-table-column prop="principal" label="主体" min-width="150" show-overflow-tooltip />
            <el-table-column label="租户" min-width="120">
              <template #default="{ row }">
                <el-tag v-if="row.tenant_id === null" type="warning" effect="plain">平台级</el-tag>
                <span v-else>{{ row.tenant_id }}</span>
              </template>
            </el-table-column>
            <el-table-column label="角色" min-width="150">
              <template #default="{ row }">
                <el-tag :type="roleTagType(row.role)" effect="plain">{{ row.role }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="granted_by" label="授权人" min-width="130" show-overflow-tooltip />
            <el-table-column label="创建时间" min-width="160">
              <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
            </el-table-column>
            <el-table-column label="操作" width="110" fixed="right">
              <template #default="{ row }">
                <el-button link type="danger" @click="confirmRevokeBinding(row)">吊销</el-button>
              </template>
            </el-table-column>
            <template #empty>
              <el-empty description="暂无角色绑定" />
            </template>
          </el-table>
        </el-tab-pane>

        <!-- ============ 跨租户授权 ============ -->
        <el-tab-pane label="跨租户授权" name="grants">
          <div class="toolbar">
            <div class="toolbar-left">
              <el-tag type="info" effect="plain">
                跨租户授权影响面为平台级：创建与吊销仅 platform_admin 可执行
              </el-tag>
              <el-tag v-if="grants" type="info" effect="plain">
                共 {{ filteredGrants.length }} 条授权
              </el-tag>
            </div>
            <div>
              <el-button :icon="Refresh" @click="refreshGrants()" :loading="grantsLoading">
                刷新
              </el-button>
              <el-button type="primary" :icon="Plus" @click="openGrantDialog()">
                新建授权
              </el-button>
            </div>
          </div>

          <ErrorState
            v-if="!grantsLoading && grantsError"
            :message="grantsError"
            @retry="refreshGrants()"
          />
          <el-table v-loading="grantsLoading" :data="filteredGrants" style="width: 100%">
            <el-table-column prop="grant_id" label="Grant ID" min-width="130" show-overflow-tooltip />
            <el-table-column prop="source_principal" label="主体" min-width="140" show-overflow-tooltip />
            <el-table-column label="源租户 → 目标租户" min-width="180">
              <template #default="{ row }">
                {{ row.source_tenant }} → {{ row.target_tenant }}
              </template>
            </el-table-column>
            <el-table-column label="资源" min-width="220">
              <template #default="{ row }">
                <el-tag
                  v-for="res in row.resources"
                  :key="res"
                  size="small"
                  effect="plain"
                  style="margin-right: 4px"
                >
                  {{ res }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="granted_by" label="授权人" min-width="130" show-overflow-tooltip />
            <el-table-column label="创建时间" min-width="160">
              <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
            </el-table-column>
            <el-table-column label="操作" width="110" fixed="right">
              <template #default="{ row }">
                <el-button link type="danger" @click="confirmRevokeGrant(row)">吊销</el-button>
              </template>
            </el-table-column>
            <template #empty>
              <el-empty description="暂无跨租户授权" />
            </template>
          </el-table>
        </el-tab-pane>
      </el-tabs>
    </el-card>

    <!-- ============ 新建绑定对话框 ============ -->
    <el-dialog v-model="bindingDialogVisible" title="新建角色绑定" width="480px">
      <el-form label-width="100px">
        <el-form-item label="主体" required>
          <el-input
            v-model="bindingForm.principal"
            placeholder="用户或服务主体标识，如 li.na / dev-svc-research"
          />
        </el-form-item>
        <el-form-item label="角色" required>
          <el-select v-model="bindingForm.role" style="width: 100%">
            <el-option
              v-for="role in ROLE_OPTIONS"
              :key="role"
              :label="role"
              :value="role"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="租户" :required="bindingForm.role !== 'platform_admin'">
          <el-select
            v-model="bindingForm.tenant_id"
            :placeholder="bindingForm.role === 'platform_admin' ? '平台级（自动）' : '选择租户'"
            :disabled="bindingForm.role === 'platform_admin'"
            clearable
            style="width: 100%"
          >
            <el-option
              v-for="tenant in knownTenants"
              :key="tenant"
              :label="tenant"
              :value="tenant"
            />
          </el-select>
          <div v-if="bindingForm.role === 'platform_admin'" class="form-hint">
            platform_admin 仅限平台级绑定
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="bindingDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="bindingSubmitting" @click="submitBinding">
          创建
        </el-button>
      </template>
    </el-dialog>

    <!-- ============ 新建授权对话框 ============ -->
    <el-dialog v-model="grantDialogVisible" title="新建跨租户授权" width="480px">
      <el-form label-width="110px">
        <el-form-item label="主体" required>
          <el-input
            v-model="grantForm.source_principal"
            placeholder="源租户下的主体标识"
          />
        </el-form-item>
        <el-form-item label="源租户" required>
          <el-select v-model="grantForm.source_tenant" style="width: 100%">
            <el-option
              v-for="tenant in knownTenants"
              :key="tenant"
              :label="tenant"
              :value="tenant"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="目标租户" required>
          <el-select v-model="grantForm.target_tenant" style="width: 100%">
            <el-option
              v-for="tenant in knownTenants"
              :key="tenant"
              :label="tenant"
              :value="tenant"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="资源" required>
          <el-input
            v-model="grantForm.resourcesText"
            type="textarea"
            :rows="3"
            placeholder="每行一个资源标识，如 policy.publish.global"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="grantDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="grantSubmitting" @click="submitGrant">
          创建
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { Plus, Refresh } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  rbacBindingSource,
  rbacGrantSource,
  type RbacBinding,
  type RbacGrant,
  type RbacRole,
} from '@/api/rbac'
import { useAsyncData, usePolling } from '@/composables/useAsyncData'
import ErrorState from '@/components/ErrorState.vue'

const ROLE_OPTIONS: RbacRole[] = [
  'platform_admin',
  'tenant_admin',
  'policy_creator',
  'policy_validator',
  'policy_publisher',
  'policy_auditor',
  'approver',
]

const activeTab = ref('bindings')

// ---------- 角色绑定 ----------
const {
  data: bindings,
  loading: bindingsLoading,
  error: bindingsError,
  refresh: refreshBindings,
} = useAsyncData(() => rbacBindingSource.listBindings(), {
  defaultErrorMessage: '角色绑定列表加载失败',
  // 加载失败由 ErrorState 持久展示，不再弹 toast
  showError: false,
})

// ---------- 跨租户授权 ----------
const {
  data: grants,
  loading: grantsLoading,
  error: grantsError,
  refresh: refreshGrants,
} = useAsyncData(() => rbacGrantSource.listGrants(), {
  defaultErrorMessage: '跨租户授权列表加载失败',
  showError: false,
})

usePolling(async () => {
  if (activeTab.value === 'bindings') await refreshBindings(true)
  else await refreshGrants(true)
}, 15000)

// ---------- 绑定过滤 ----------
const bindingTenantFilter = ref('')
const bindingRoleFilter = ref('')

const filteredBindings = computed<RbacBinding[]>(() => {
  if (!bindings.value) return []
  return bindings.value.filter(
    (item) =>
      (!bindingTenantFilter.value || item.tenant_id === bindingTenantFilter.value) &&
      (!bindingRoleFilter.value || item.role === bindingRoleFilter.value),
  )
})

const bindingTenantOptions = computed(() => {
  const tenants = new Set(
    (bindings.value ?? [])
      .map((item) => item.tenant_id)
      .filter((tenant): tenant is string => tenant !== null),
  )
  return [...tenants]
})

// ---------- 授权列表 ----------
const filteredGrants = computed<RbacGrant[]>(() => grants.value ?? [])

// 已知租户 = 绑定与授权中动态推导，供新建表单下拉使用
const knownTenants = computed(() => {
  const tenants = new Set<string>()
  for (const item of bindings.value ?? []) {
    if (item.tenant_id) tenants.add(item.tenant_id)
  }
  for (const item of grants.value ?? []) {
    tenants.add(item.source_tenant)
    tenants.add(item.target_tenant)
  }
  return [...tenants]
})

// ---------- 新建绑定 ----------
const bindingDialogVisible = ref(false)
const bindingSubmitting = ref(false)
const bindingForm = reactive<{ principal: string; tenant_id: string; role: RbacRole }>({
  principal: '',
  tenant_id: '',
  role: 'tenant_admin',
})

function openBindingDialog() {
  bindingForm.principal = ''
  bindingForm.tenant_id = ''
  bindingForm.role = 'tenant_admin'
  bindingDialogVisible.value = true
}

async function submitBinding() {
  if (!bindingForm.principal.trim()) {
    ElMessage.warning('请填写主体标识')
    return
  }
  if (bindingForm.role !== 'platform_admin' && !bindingForm.tenant_id) {
    ElMessage.warning('租户角色必须选择租户')
    return
  }
  bindingSubmitting.value = true
  try {
    const created = await rbacBindingSource.createBinding({
      principal: bindingForm.principal.trim(),
      tenant_id: bindingForm.role === 'platform_admin' ? null : bindingForm.tenant_id,
      role: bindingForm.role,
    })
    ElMessage.success(`绑定已创建：${created.binding_id}`)
    bindingDialogVisible.value = false
    await refreshBindings(true)
  } catch (e: any) {
    ElMessage.error(e?.message || '创建失败')
  } finally {
    bindingSubmitting.value = false
  }
}

// ---------- 吊销绑定 ----------
async function confirmRevokeBinding(row: RbacBinding) {
  const scope = row.tenant_id === null ? '平台级' : `租户 ${row.tenant_id}`
  try {
    await ElMessageBox.confirm(
      `确认吊销 ${row.principal} 的 ${row.role} 绑定（${scope}）？吊销后立即失效并退出列表。`,
      '吊销角色绑定',
      { type: 'warning', confirmButtonText: '吊销', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await rbacBindingSource.revokeBinding(row.binding_id)
    ElMessage.success('绑定已吊销')
    await refreshBindings(true)
  } catch (e: any) {
    ElMessage.error(e?.message || '吊销失败')
  }
}

// ---------- 新建授权 ----------
const grantDialogVisible = ref(false)
const grantSubmitting = ref(false)
const grantForm = reactive<{
  source_principal: string
  source_tenant: string
  target_tenant: string
  resourcesText: string
}>({
  source_principal: '',
  source_tenant: '',
  target_tenant: '',
  resourcesText: '',
})

function openGrantDialog() {
  grantForm.source_principal = ''
  grantForm.source_tenant = ''
  grantForm.target_tenant = ''
  grantForm.resourcesText = ''
  grantDialogVisible.value = true
}

async function submitGrant() {
  const resources = grantForm.resourcesText
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
  if (!grantForm.source_principal.trim() || !grantForm.source_tenant || !grantForm.target_tenant) {
    ElMessage.warning('请完整填写主体与源/目标租户')
    return
  }
  if (!resources.length) {
    ElMessage.warning('请至少填写一个资源标识')
    return
  }
  grantSubmitting.value = true
  try {
    const created = await rbacGrantSource.createGrant({
      source_principal: grantForm.source_principal.trim(),
      source_tenant: grantForm.source_tenant,
      target_tenant: grantForm.target_tenant,
      resources,
    })
    ElMessage.success(`授权已创建：${created.grant_id}`)
    grantDialogVisible.value = false
    await refreshGrants(true)
  } catch (e: any) {
    ElMessage.error(e?.message || '创建失败')
  } finally {
    grantSubmitting.value = false
  }
}

// ---------- 吊销授权 ----------
async function confirmRevokeGrant(row: RbacGrant) {
  try {
    await ElMessageBox.confirm(
      `确认吊销 ${row.source_principal}（${row.source_tenant} → ${row.target_tenant}）的跨租户授权？吊销后立即失效并退出列表。`,
      '吊销跨租户授权',
      { type: 'warning', confirmButtonText: '吊销', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await rbacGrantSource.revokeGrant(row.grant_id)
    ElMessage.success('授权已吊销')
    await refreshGrants(true)
  } catch (e: any) {
    ElMessage.error(e?.message || '吊销失败')
  }
}

// ---------- 展示辅助 ----------
function roleTagType(role: string): 'success' | 'warning' | 'danger' | 'info' {
  if (role === 'platform_admin') return 'danger'
  if (role === 'tenant_admin') return 'warning'
  if (role === 'approver') return 'success'
  return 'info'
}

function formatTime(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}
</script>

<style scoped>
.toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
  gap: 12px;
}

.toolbar-left {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.label {
  font-size: 14px;
  color: #606266;
}

.form-hint {
  font-size: 12px;
  color: #909399;
  line-height: 1.4;
  margin-top: 4px;
}
</style>
