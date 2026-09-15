<template>
  <div>
    <el-card shadow="hover">
      <template #header>
        <span>工具策略</span>
      </template>
      <el-collapse v-model="activeProfiles">
        <el-collapse-item
          v-for="profile in profiles"
          :key="profile.profile_id"
          :title="`${profile.profile_id} - ${profile.description || '无描述'}`"
          :name="profile.profile_id"
        >
          <el-descriptions :column="3" border>
            <el-descriptions-item label="Token 预算">{{ profile.max_budget_token }}</el-descriptions-item>
            <el-descriptions-item label="支付预算">{{ profile.max_budget_payment }}</el-descriptions-item>
            <el-descriptions-item label="工具数量">{{ Object.keys(profile.tools || {}).length }}</el-descriptions-item>
          </el-descriptions>
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
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { loadProfilesConfig } from '@/api/config'

const profiles = ref<any[]>([])
const activeProfiles = ref<string[]>([])

function formatTools(tools: Record<string, any>) {
  return Object.entries(tools || {}).map(([name, policy]) => ({
    name,
    ...policy,
    allowed_args: policy.allowed_args ? JSON.stringify(policy.allowed_args) : '-',
  }))
}

async function loadData() {
  try {
    const config = await loadProfilesConfig()
    profiles.value = config.profiles || []
    activeProfiles.value = profiles.value.map((p) => p.profile_id)
  } catch (error: any) {
    ElMessage.error(error.message || '加载工具策略失败')
  }
}

onMounted(loadData)
</script>

<style scoped>
.tool-table {
  margin-top: 16px;
}
</style>
