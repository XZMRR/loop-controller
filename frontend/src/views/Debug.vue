<template>
  <div>
    <el-card shadow="hover">
      <template #header>
        <div class="card-header">
          <span>判定调试（Dry-Run）</span>
          <el-tag type="info" size="small">只读判定，不会真实执行工具</el-tag>
        </div>
      </template>
      <el-form label-position="top">
        <el-row :gutter="16">
          <el-col :span="8">
            <el-form-item label="Agent">
              <el-select v-model="form.agent_id" placeholder="选择 Agent" filterable class="full">
                <el-option
                  v-for="a in agents"
                  :key="a.agent_id"
                  :label="`${a.agent_id}（${a.name || '未命名'}）`"
                  :value="a.agent_id"
                />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="用户 ID">
              <el-input v-model="form.user_id" placeholder="如 alice" />
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="工具名">
              <el-select
                v-model="form.tool_name"
                placeholder="选择或输入工具"
                filterable
                allow-create
                class="full"
              >
                <el-option v-for="t in toolNames" :key="t" :label="t" :value="t" />
              </el-select>
            </el-form-item>
          </el-col>
        </el-row>
        <el-form-item label="参数（JSON 对象）">
          <el-input
            v-model="argumentsText"
            type="textarea"
            :rows="4"
            placeholder='{"to": "zhang@company.com"}'
          />
        </el-form-item>
        <el-form-item label="任务上下文（可选）">
          <el-input v-model="form.task_context" type="textarea" :rows="2" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="loading" @click="handleEvaluate">执行判定</el-button>
          <el-button @click="handleReset">清空</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <el-card v-if="result" class="mt-4" shadow="hover">
      <template #header>
        <div class="card-header">
          <span>判定结果</span>
          <el-tag v-if="result.dry_run" type="info" size="small">dry-run</el-tag>
        </div>
      </template>
      <el-descriptions :column="2" border>
        <el-descriptions-item label="裁决">
          <el-tag :type="verdictTagType">{{ result.verdict }}</el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="风险等级">
          <el-tag v-if="result.risk_level" :type="riskTagType">{{ result.risk_level }}</el-tag>
          <span v-else>-</span>
        </el-descriptions-item>
        <el-descriptions-item label="原因" :span="2">{{ result.reason || '-' }}</el-descriptions-item>
        <el-descriptions-item label="命中策略" :span="2">
          <template v-if="result.policy_hits?.length">
            <el-tag v-for="hit in result.policy_hits" :key="hit" size="small" class="tag-item">
              {{ hit }}
            </el-tag>
          </template>
          <span v-else>-</span>
        </el-descriptions-item>
        <el-descriptions-item label="风险标签" :span="2">
          <template v-if="result.risk_tags?.length">
            <el-tag
              v-for="tag in result.risk_tags"
              :key="tag"
              type="warning"
              size="small"
              class="tag-item"
            >
              {{ tag }}
            </el-tag>
          </template>
          <span v-else>-</span>
        </el-descriptions-item>
        <el-descriptions-item label="策略版本">{{ result.policy_version || '-' }}</el-descriptions-item>
        <el-descriptions-item label="Profile 版本">{{ result.profile_version || '-' }}</el-descriptions-item>
      </el-descriptions>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { evaluateGovern, type GovernEvaluateResponse } from '@/api/python'
import { loadAgentsConfig, loadProfilesConfig } from '@/api/config'

const agents = ref<any[]>([])
const toolNames = ref<string[]>([])
const form = reactive({ agent_id: '', user_id: '', tool_name: '', task_context: '' })
const argumentsText = ref('{}')
const loading = ref(false)
const result = ref<GovernEvaluateResponse | null>(null)

const verdictTagType = computed(() => {
  switch (result.value?.verdict) {
    case 'allow':
      return 'success'
    case 'deny':
    case 'blocked':
      return 'danger'
    case 'require_approval':
    case 'modify':
      return 'warning'
    default:
      return 'info'
  }
})

const riskTagType = computed(() => {
  switch (result.value?.risk_level) {
    case 'critical':
    case 'high':
      return 'danger'
    case 'medium':
      return 'warning'
    default:
      return 'success'
  }
})

async function loadData() {
  // 下拉选项加载失败不阻塞手填
  try {
    const [agentCfg, profileCfg] = await Promise.all([loadAgentsConfig(), loadProfilesConfig()])
    agents.value = agentCfg.agents || []
    const names = new Set<string>()
    for (const p of profileCfg.profiles || []) {
      for (const t of Object.keys(p.tools || {})) names.add(t)
    }
    toolNames.value = [...names].sort()
  } catch {
    /* 静默：表单仍可手动填写 */
  }
}

async function handleEvaluate() {
  if (!form.agent_id || !form.user_id || !form.tool_name) {
    ElMessage.warning('请填写 Agent、用户 ID 与工具名')
    return
  }
  let args: Record<string, any>
  try {
    const parsed = JSON.parse(argumentsText.value || '{}')
    if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
      throw new Error('参数必须是 JSON 对象')
    }
    args = parsed
  } catch (e: any) {
    ElMessage.error(`参数 JSON 解析失败：${e.message}`)
    return
  }
  loading.value = true
  try {
    result.value = await evaluateGovern({
      agent_id: form.agent_id,
      user_id: form.user_id,
      tool_name: form.tool_name,
      arguments: args,
      task_context: form.task_context,
    })
  } catch (error: any) {
    ElMessage.error(error.response?.data?.error || error.message || '判定请求失败')
  } finally {
    loading.value = false
  }
}

function handleReset() {
  form.agent_id = ''
  form.user_id = ''
  form.tool_name = ''
  form.task_context = ''
  argumentsText.value = '{}'
  result.value = null
}

onMounted(loadData)
</script>

<style scoped>
.mt-4 {
  margin-top: 16px;
}

.card-header {
  display: flex;
  align-items: center;
  gap: 8px;
}

.full {
  width: 100%;
}

.tag-item {
  margin-right: 8px;
}
</style>
