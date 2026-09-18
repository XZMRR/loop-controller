<template>
  <div>
    <!-- ============ 生命周期状态卡（独立加载，失败不影响列表） ============ -->
    <div class="status-row" v-loading="statusLoading">
      <el-card v-if="status" shadow="never" class="status-card">
        <div class="status-label">期望版本</div>
        <div class="status-value">{{ status.expected_revision ?? '—' }}</div>
      </el-card>
      <el-card v-if="status" shadow="never" class="status-card">
        <div class="status-label">活跃版本</div>
        <div class="status-value">
          {{ status.active_revision ?? '未全员加载' }}
        </div>
      </el-card>
      <el-card v-if="status" shadow="never" class="status-card">
        <div class="status-label">Generation</div>
        <div class="status-value">{{ status.generation }}</div>
      </el-card>
      <el-card v-if="status" shadow="never" class="status-card">
        <div class="status-label">实例加载</div>
        <div class="status-value">
          {{ status.loaded_instances }}/{{ status.required_instances }}
        </div>
        <div v-if="status.stale_instances.length || status.error_instances.length" class="status-warn">
          <span v-if="status.stale_instances.length">stale: {{ status.stale_instances.join(', ') }}</span>
          <span v-if="status.error_instances.length">error: {{ status.error_instances.join(', ') }}</span>
        </div>
      </el-card>
    </div>

    <el-card>
      <el-tabs v-model="activeTab">
        <!-- ============ 候选列表 ============ -->
        <el-tab-pane label="策略候选" name="candidates">
          <div class="toolbar">
            <div class="toolbar-left">
              <el-tag v-if="candidates" type="info" effect="plain">
                共 {{ filteredCandidates.length }} 个候选
              </el-tag>
              <el-tag type="info" effect="plain">
                生命周期：draft → validated → published → loaded（可回滚）
              </el-tag>
            </div>
            <el-button :icon="Refresh" @click="refreshAll()" :loading="candidatesLoading">
              刷新
            </el-button>
          </div>

          <ErrorState
            v-if="!candidatesLoading && candidatesError"
            :message="candidatesError"
            @retry="refreshCandidates()"
          />
          <el-table v-loading="candidatesLoading" :data="filteredCandidates" style="width: 100%">
            <el-table-column prop="candidate_id" label="Candidate ID" min-width="120" show-overflow-tooltip />
            <el-table-column prop="revision" label="Revision" min-width="100" />
            <el-table-column label="状态" min-width="110">
              <template #default="{ row }">
                <el-tag :type="stateTagType(row.state)" effect="plain">{{ row.state }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="租户" min-width="100">
              <template #default="{ row }">
                <el-tag v-if="row.tenant_id === null" type="warning" effect="plain">平台级</el-tag>
                <span v-else>{{ row.tenant_id }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="created_by" label="创建人" min-width="120" show-overflow-tooltip />
            <el-table-column label="创建时间" min-width="160">
              <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
            </el-table-column>
            <el-table-column label="发布时间" min-width="160">
              <template #default="{ row }">
                {{ row.published_at ? formatTime(row.published_at) : '—' }}
              </template>
            </el-table-column>
            <el-table-column label="操作" width="250" fixed="right">
              <template #default="{ row }">
                <el-button link type="primary" @click="openDetail(row)">详情</el-button>
                <el-button
                  v-if="row.state === 'draft'"
                  link
                  type="warning"
                  @click="confirmValidate(row)"
                >
                  校验
                </el-button>
                <el-button
                  v-if="['validated', 'published', 'loaded'].includes(row.state)"
                  link
                  @click="openShadowDialog(row)"
                >
                  影子
                </el-button>
                <el-button
                  v-if="row.state === 'validated'"
                  link
                  type="success"
                  @click="confirmPublish(row)"
                >
                  发布
                </el-button>
                <el-button
                  v-if="row.revision !== status?.expected_revision && ['published', 'loaded', 'superseded'].includes(row.state)"
                  link
                  type="danger"
                  @click="confirmRollback(row)"
                >
                  回滚到此
                </el-button>
              </template>
            </el-table-column>
            <template #empty>
              <el-empty description="暂无策略候选" />
            </template>
          </el-table>
        </el-tab-pane>

        <!-- ============ 操作审计 ============ -->
        <el-tab-pane label="操作审计" name="audit">
          <div class="toolbar">
            <el-tag v-if="auditEvents" type="info" effect="plain">
              共 {{ auditEvents.length }} 条事件
            </el-tag>
            <el-button :icon="Refresh" @click="refreshAudit()" :loading="auditLoading">
              刷新
            </el-button>
          </div>
          <ErrorState
            v-if="!auditLoading && auditError"
            :message="auditError"
            @retry="refreshAudit()"
          />
          <el-table v-loading="auditLoading" :data="auditEvents ?? []" style="width: 100%">
            <el-table-column prop="action" label="动作" min-width="180" />
            <el-table-column prop="target" label="目标" min-width="130" show-overflow-tooltip />
            <el-table-column prop="actor" label="操作人" min-width="130" show-overflow-tooltip />
            <el-table-column label="时间" min-width="160">
              <template #default="{ row }">{{ row.created_at ? formatTime(row.created_at) : '—' }}</template>
            </el-table-column>
            <el-table-column label="详情" min-width="220">
              <template #default="{ row }">
                <pre v-if="row.metadata" class="json-inline">{{ JSON.stringify(row.metadata) }}</pre>
                <span v-else>—</span>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>
      </el-tabs>
    </el-card>

    <!-- ============ 候选详情抽屉 ============ -->
    <el-drawer v-model="detailVisible" title="候选详情" size="520px">
      <template v-if="detail">
        <el-descriptions :column="1" border>
          <el-descriptions-item label="Candidate ID">{{ detail.candidate_id }}</el-descriptions-item>
          <el-descriptions-item label="状态">
            <el-tag :type="stateTagType(detail.state)" effect="plain">{{ detail.state }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="Revision">{{ detail.revision }}</el-descriptions-item>
          <el-descriptions-item label="Base Revision">{{ detail.base_revision ?? '—' }}</el-descriptions-item>
          <el-descriptions-item label="Source SHA256">
            <span class="mono">{{ detail.source_sha256 }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="Artifact SHA256">
            <span class="mono">{{ detail.artifact_sha256 }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="Artifact 大小">{{ detail.artifact_size }} bytes</el-descriptions-item>
          <el-descriptions-item label="创建人">{{ detail.created_by }}</el-descriptions-item>
          <el-descriptions-item label="创建时间">{{ formatTime(detail.created_at) }}</el-descriptions-item>
          <el-descriptions-item label="发布时间">
            {{ detail.published_at ? formatTime(detail.published_at) : '—' }}
          </el-descriptions-item>
          <el-descriptions-item v-if="detail.rollback_of_revision" label="回滚来源版本">
            {{ detail.rollback_of_revision }}
          </el-descriptions-item>
        </el-descriptions>
        <template v-if="detailValidation">
          <div class="section-title">校验结果（{{ detailValidation.ok ? '通过' : '失败' }}）</div>
          <el-table :data="detailValidation.stages" size="small">
            <el-table-column prop="stage" label="阶段" width="120" />
            <el-table-column label="结果" width="80">
              <template #default="{ row }">
                <el-tag :type="row.ok ? 'success' : 'danger'" size="small" effect="plain">
                  {{ row.ok ? '通过' : '失败' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="output_summary" label="说明" min-width="200" show-overflow-tooltip />
            <el-table-column prop="elapsed_ms" label="耗时" width="90">
              <template #default="{ row }">{{ row.elapsed_ms }}ms</template>
            </el-table-column>
          </el-table>
          <div v-if="detailValidation.failure_code" class="section-warn">
            failure_code: {{ detailValidation.failure_code }}
          </div>
        </template>
      </template>
    </el-drawer>

    <!-- ============ 影子运行对话框 ============ -->
    <el-dialog v-model="shadowDialogVisible" title="影子运行（与线上基线对比）" width="640px">
      <template v-if="shadowCandidate">
        <el-alert type="info" :closable="false" class="shadow-alert">
          候选 {{ shadowCandidate.candidate_id }}（{{ shadowCandidate.revision }}）：
          对样本分别用线上基线与候选策略求值并比对判定差异，不影响线上。
        </el-alert>
        <el-form label-width="100px" style="margin-top: 12px">
          <el-form-item label="样本集 (JSON)">
            <el-input
              v-model="shadowSamplesText"
              type="textarea"
              :rows="8"
              placeholder='[{"sample_id":"s1","package":"governance.interaction","input":{...},"redaction_attestation":{}}]'
            />
          </el-form-item>
        </el-form>
        <template v-if="shadowResult">
          <div class="section-title">
            结果：{{ shadowResult.total }} 个样本，相同 {{ shadowResult.same_count }}，
            变化 {{ shadowResult.changed_count }}
            <span v-if="shadowResult.allow_to_deny" class="section-warn-inline">
              （allow→deny {{ shadowResult.allow_to_deny }}）
            </span>
            <span v-if="shadowResult.deny_to_allow" class="section-warn-inline">
              （deny→allow {{ shadowResult.deny_to_allow }}）
            </span>
          </div>
          <el-table :data="shadowResult.items" size="small" max-height="240">
            <el-table-column prop="sample_id" label="Sample" min-width="120" show-overflow-tooltip />
            <el-table-column label="基线" width="90">
              <template #default="{ row }">
                <el-tag size="small" effect="plain">{{ row.baseline_verdict }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="候选" width="90">
              <template #default="{ row }">
                <el-tag :type="row.same ? 'success' : 'danger'" size="small" effect="plain">
                  {{ row.candidate_verdict }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="一致" width="80">
              <template #default="{ row }">
                <el-tag :type="row.same ? 'success' : 'danger'" size="small">
                  {{ row.same ? '是' : '否' }}
                </el-tag>
              </template>
            </el-table-column>
          </el-table>
        </template>
      </template>
      <template #footer>
        <el-button @click="shadowDialogVisible = false">关闭</el-button>
        <el-button type="primary" :loading="shadowRunning" @click="runShadow">
          运行
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  policySource,
  type PolicyCandidate,
  type PolicyShadowRunResult,
  type PolicyStatus,
  type PolicyValidationResult,
} from '@/api/policy'
import { useAsyncData, usePolling } from '@/composables/useAsyncData'
import ErrorState from '@/components/ErrorState.vue'

const activeTab = ref('candidates')

// ---------- 生命周期状态（独立加载） ----------
const status = ref<PolicyStatus | null>(null)
const statusLoading = ref(false)

async function loadStatus(silent = false) {
  if (!silent) statusLoading.value = true
  try {
    status.value = await policySource.getStatus()
  } catch {
    // 静默失败：状态卡不可达不影响候选列表
  } finally {
    statusLoading.value = false
  }
}

// ---------- 候选列表 ----------
const {
  data: candidates,
  loading: candidatesLoading,
  error: candidatesError,
  refresh: refreshCandidates,
} = useAsyncData(() => policySource.listCandidates(), {
  defaultErrorMessage: '候选列表加载失败',
  // 加载失败由 ErrorState 持久展示，不再弹 toast
  showError: false,
})

// ---------- 审计 ----------
const {
  data: auditEvents,
  loading: auditLoading,
  error: auditError,
  refresh: refreshAudit,
} = useAsyncData(() => policySource.listAudit(), {
  defaultErrorMessage: '审计列表加载失败',
  showError: false,
})

usePolling(async () => {
  await loadStatus(true)
  if (activeTab.value === 'candidates') await refreshCandidates(true)
  else await refreshAudit(true)
}, 15000)

loadStatus()

const filteredCandidates = computed<PolicyCandidate[]>(
  () => candidates.value ?? [],
)

async function refreshAll() {
  await Promise.all([refreshCandidates(), loadStatus()])
}

// ---------- 详情 ----------
const detailVisible = ref(false)
const detail = ref<PolicyCandidate | null>(null)
const detailValidation = ref<PolicyValidationResult | null>(null)
// 本页会话内已产生的校验结果（validateCandidate 有状态副作用，详情抽屉不得触发）
const knownValidations = new Map<string, PolicyValidationResult>()

function openDetail(row: PolicyCandidate) {
  detail.value = row
  detailValidation.value = knownValidations.get(row.candidate_id) ?? null
  detailVisible.value = true
}

// ---------- 校验 ----------
async function confirmValidate(row: PolicyCandidate) {
  try {
    await ElMessageBox.confirm(
      `对候选 ${row.candidate_id}（${row.revision}）执行校验？draft 状态将变为 validated 或 failed。`,
      '执行校验',
      { type: 'info', confirmButtonText: '校验', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    const result = await policySource.validateCandidate(row.candidate_id)
    if (result.validation) {
      knownValidations.set(row.candidate_id, result.validation)
    }
    if (result.validation?.ok) {
      ElMessage.success('校验通过，候选已置为 validated')
    } else {
      ElMessage.error(`校验失败：${result.validation?.failure_code ?? '未知原因'}`)
    }
    await refreshAll()
  } catch (e: any) {
    ElMessage.error(e?.message || '校验失败')
  }
}

// ---------- 影子运行 ----------
const shadowDialogVisible = ref(false)
const shadowCandidate = ref<PolicyCandidate | null>(null)
const shadowSamplesText = ref('')
const shadowRunning = ref(false)
const shadowResult = ref<PolicyShadowRunResult | null>(null)

const SHADOW_SAMPLE_PLACEHOLDER = JSON.stringify(
  [
    {
      sample_id: 's1',
      package: 'governance.interaction',
      input: { agent_id: 'researcher_001', tool_name: 'web_search' },
      redaction_attestation: {},
    },
    {
      sample_id: 's2',
      package: 'governance.interaction',
      input: { agent_id: 'researcher_001', tool_name: 'send_email', force_deny: true },
      redaction_attestation: {},
    },
  ],
  null,
  2,
)

function openShadowDialog(row: PolicyCandidate) {
  shadowCandidate.value = row
  shadowResult.value = null
  shadowSamplesText.value = SHADOW_SAMPLE_PLACEHOLDER
  shadowDialogVisible.value = true
}

async function runShadow() {
  if (!shadowCandidate.value) return
  let samples: any[]
  try {
    const parsed = JSON.parse(shadowSamplesText.value || '[]')
    if (!Array.isArray(parsed)) throw new Error('not array')
    samples = parsed
  } catch {
    ElMessage.error('样本集不是合法 JSON 数组')
    return
  }
  shadowRunning.value = true
  try {
    shadowResult.value = await policySource.runShadow(shadowCandidate.value.candidate_id, {
      revision: shadowCandidate.value.revision,
      artifact_sha256: shadowCandidate.value.artifact_sha256,
      samples,
    })
    ElMessage.success('影子运行完成')
    await refreshAudit(true)
  } catch (e: any) {
    ElMessage.error(e?.message || '影子运行失败')
  } finally {
    shadowRunning.value = false
  }
}

// ---------- 发布 ----------
async function confirmPublish(row: PolicyCandidate) {
  try {
    await ElMessageBox.confirm(
      `发布候选 ${row.candidate_id}（${row.revision}）？发布后该版本成为期望版本，OPA 实例将异步加载。`,
      '发布策略',
      { type: 'warning', confirmButtonText: '发布', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    const result = await policySource.publishCandidate(row.candidate_id, {
      base_revision: row.base_revision,
    })
    ElMessage.success(`已发布：${result.revision}（generation ${result.generation}）`)
    await refreshAll()
  } catch (e: any) {
    ElMessage.error(e?.message || '发布失败')
  }
}

// ---------- 回滚 ----------
async function confirmRollback(row: PolicyCandidate) {
  try {
    await ElMessageBox.confirm(
      `回滚到 ${row.revision}（候选 ${row.candidate_id}）？当前期望版本 ${status.value?.expected_revision ?? '—'} 将被替换，影响全部 OPA 实例。`,
      '回滚策略',
      { type: 'error', confirmButtonText: '回滚', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    const result = await policySource.rollbackPolicy({ revision: row.revision })
    ElMessage.success(`已回滚：${result.revision}（generation ${result.generation}）`)
    await refreshAll()
  } catch (e: any) {
    ElMessage.error(e?.message || '回滚失败')
  }
}

// ---------- 展示辅助 ----------
function stateTagType(state: string): 'success' | 'warning' | 'danger' | 'info' {
  switch (state) {
    case 'loaded':
      return 'success'
    case 'published':
      return 'success'
    case 'validated':
      return 'warning'
    case 'draft':
      return 'info'
    default:
      return 'danger'
  }
}

function formatTime(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}
</script>

<style scoped>
.status-row {
  display: flex;
  gap: 16px;
  margin-bottom: 16px;
}

.status-card {
  flex: 1;
}

.status-label {
  font-size: 13px;
  color: #909399;
  margin-bottom: 6px;
}

.status-value {
  font-size: 18px;
  font-weight: 600;
}

.status-warn {
  margin-top: 6px;
  font-size: 12px;
  color: #e6a23c;
  display: flex;
  gap: 10px;
}

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
  flex-wrap: wrap;
}

.section-title {
  margin: 16px 0 8px;
  font-size: 14px;
  font-weight: 600;
}

.section-warn {
  margin-top: 8px;
  font-size: 12px;
  color: #f56c6c;
}

.section-warn-inline {
  color: #f56c6c;
}

.shadow-alert {
  margin-bottom: 4px;
}

.mono {
  font-family: monospace;
  font-size: 12px;
  word-break: break-all;
}

.json-inline {
  margin: 0;
  font-size: 12px;
  font-family: monospace;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
