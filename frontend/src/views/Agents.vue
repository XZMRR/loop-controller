<template>
  <div>
    <el-card shadow="hover">
      <template #header>
        <span>已注册 Agent</span>
      </template>
      <el-table :data="agents" stripe>
        <el-table-column prop="agent_id" label="Agent ID" width="180" />
        <el-table-column prop="name" label="名称" width="180" />
        <el-table-column prop="profile_id" label="Profile" width="180" />
        <el-table-column prop="owner_id" label="Owner" />
        <el-table-column label="状态" width="120">
          <template #default>
            <el-tag type="success">正常</el-tag>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-card class="mt-4" shadow="hover">
      <template #header>
        <span>已注册用户</span>
      </template>
      <el-table :data="users" stripe>
        <el-table-column prop="user_id" label="User ID" />
        <el-table-column prop="display_name" label="显示名" />
      </el-table>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { loadAgentsConfig } from '@/api/config'

const agents = ref<any[]>([])
const users = ref<any[]>([])

async function loadData() {
  try {
    const config = await loadAgentsConfig()
    agents.value = config.agents || []
    users.value = config.users || []
  } catch (error: any) {
    ElMessage.error(error.message || '加载 Agent 配置失败')
  }
}

onMounted(loadData)
</script>

<style scoped>
.mt-4 {
  margin-top: 16px;
}
</style>
