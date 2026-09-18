<template>
  <div>
    <el-card shadow="hover">
      <template #header>
        <div class="card-header">
          <span>已注册 Agent</span>
          <el-button size="small" @click="loadData">刷新</el-button>
        </div>
      </template>
      <ErrorState
        v-if="!loading && loadError"
        :message="loadError"
        @retry="loadData"
      />
      <el-table :data="agents" v-loading="loading" stripe>
        <el-table-column prop="agent_id" label="Agent ID" width="180" />
        <el-table-column prop="name" label="名称" width="180" />
        <el-table-column prop="profile_id" label="Profile" width="180" />
        <el-table-column prop="owner_id" label="Owner" />
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="row.revoked ? 'danger' : 'success'">
              {{ row.revoked ? '已吊销' : '正常' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="100">
          <template #default="{ row }">
            <el-button size="small" link type="primary" @click="openDetail(row)">详情</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="暂无已注册 Agent" />
        </template>
      </el-table>
    </el-card>

    <el-drawer v-model="detailVisible" :title="detail?.name || 'Agent 详情'" size="420px">
      <el-skeleton v-if="detailLoading" :rows="6" animated />
      <template v-else-if="detail">
        <el-descriptions :column="1" border>
          <el-descriptions-item label="Agent ID">{{ detail.agent_id }}</el-descriptions-item>
          <el-descriptions-item label="名称">{{ detail.name }}</el-descriptions-item>
          <el-descriptions-item label="Profile">{{ detail.profile_id }}</el-descriptions-item>
          <el-descriptions-item label="Owner">{{ detail.owner_name || detail.owner_id }}</el-descriptions-item>
          <el-descriptions-item label="Tenant">{{ detail.tenant_id || '-' }}</el-descriptions-item>
          <el-descriptions-item label="状态">
            <el-tag :type="detail.revoked ? 'danger' : 'success'">
              {{ detail.revoked ? '已吊销' : '正常' }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item v-if="detail.description" label="描述">
            {{ detail.description }}
          </el-descriptions-item>
        </el-descriptions>
        <template v-if="detail.metadata && Object.keys(detail.metadata).length">
          <h4 class="mt-4">扩展元数据</h4>
          <pre class="metadata">{{ JSON.stringify(detail.metadata, null, 2) }}</pre>
        </template>
      </template>
      <el-empty v-else description="详情不可用" />
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { loadAgentsConfig } from '@/api/config'
import { getAdminAgentDetail, type AdminAgentDetail } from '@/api/python'
import ErrorState from '@/components/ErrorState.vue'

const agents = ref<any[]>([])
const loading = ref(false)
// 加载失败的持久错误态（由 ErrorState 展示，含重试）
const loadError = ref('')

const detailVisible = ref(false)
const detailLoading = ref(false)
const detail = ref<AdminAgentDetail | null>(null)

async function loadData() {
  loading.value = true
  try {
    const config = await loadAgentsConfig()
    agents.value = config.agents
    loadError.value = ''
  } catch (error: any) {
    loadError.value = error.message || '加载 Agent 配置失败'
  } finally {
    loading.value = false
  }
}

async function openDetail(row: any) {
  detailVisible.value = true
  detailLoading.value = true
  detail.value = { ...row }
  try {
    detail.value = await getAdminAgentDetail(row.agent_id)
  } catch {
    // 后端未运行时退回列表行数据
  } finally {
    detailLoading.value = false
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

.metadata {
  background: #f5f7fa;
  border-radius: 4px;
  padding: 12px;
  font-size: 12px;
  overflow: auto;
}
</style>
