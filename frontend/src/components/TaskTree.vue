<template>
  <div>
    <el-tree
      :data="treeData"
      node-key="task_id"
      :props="{ label: 'task_id', children: 'children' }"
      default-expand-all
      :expand-on-click-node="false"
      highlight-current
      @node-click="handleNodeClick"
    >
      <template #default="{ data }">
        <div class="tree-node">
          <span class="node-agents">
            {{ data.initiator_agent_id }}
            <el-icon class="arrow"><Right /></el-icon>
            {{ data.target_agent_id }}
          </span>
          <el-tag :type="statusTagType(data.status)" size="small" effect="dark">
            {{ data.status }}
          </el-tag>
          <el-tag v-if="data.depth > 0" size="small" type="info">depth {{ data.depth }}</el-tag>
          <span v-if="data.consumed_budget" class="node-consumed">
            consumed {{ data.consumed_budget.token_count }}/{{ data.budget.token_count }}
          </span>
        </div>
      </template>
    </el-tree>

    <el-drawer v-model="detailVisible" title="任务详情" size="480px">
      <template v-if="selected">
        <el-descriptions :column="1" border>
          <el-descriptions-item label="Task ID">
            <span class="mono">{{ selected.task_id }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="委托">
            {{ selected.initiator_agent_id }} → {{ selected.target_agent_id }}
          </el-descriptions-item>
          <el-descriptions-item label="状态">
            <el-tag :type="statusTagType(selected.status)" size="small">{{ selected.status }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="Depth">{{ selected.depth }}</el-descriptions-item>
          <el-descriptions-item label="Root Task">
            <span class="mono">{{ selected.root_task_id }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="Scope">
            <el-tag
              v-for="s in selected.scope"
              :key="s"
              size="small"
              style="margin-right: 4px"
            >{{ s }}</el-tag>
            <span v-if="selected.scope.length === 0">-</span>
          </el-descriptions-item>
          <el-descriptions-item label="预算">
            {{ selected.budget.token_count }} tokens
            <span v-if="selected.consumed_budget">
              / 已消耗 {{ selected.consumed_budget.token_count }}
            </span>
          </el-descriptions-item>
          <el-descriptions-item label="允许再委托">
            {{ selected.allow_redelegation ? '是' : '否（防扩大收窄）' }}
          </el-descriptions-item>
          <el-descriptions-item v-if="selected.error_code" label="错误码">
            <el-tag type="danger" size="small">{{ selected.error_code }}</el-tag>
          </el-descriptions-item>
        </el-descriptions>
        <div v-if="selected.outcome" class="outcome-block">
          <div class="outcome-title">Outcome</div>
          <pre>{{ JSON.stringify(selected.outcome, null, 2) }}</pre>
        </div>
      </template>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import { Right } from '@element-plus/icons-vue'
import type { A2ATaskNode } from '@/api/a2a'

const props = defineProps<{
  trees: A2ATaskNode[]
}>()

const detailVisible = ref(false)
const selected = ref<A2ATaskNode | null>(null)

const treeData = computed(() => props.trees)

function handleNodeClick(node: A2ATaskNode) {
  selected.value = node
  detailVisible.value = true
}

function statusTagType(status: string) {
  if (status === 'completed') return 'success'
  if (status === 'failed' || status === 'rejected') return 'danger'
  if (status === 'running') return 'primary'
  if (status === 'canceled' || status === 'cancelled') return 'info'
  return 'warning'
}
</script>

<style scoped>
.tree-node {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.node-agents {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 13px;
}

.arrow {
  color: #909399;
}

.node-consumed {
  color: #909399;
  font-size: 12px;
}

.mono {
  font-family: 'Courier New', monospace;
  font-size: 12px;
  word-break: break-all;
}

.outcome-block {
  margin-top: 16px;
}

.outcome-title {
  font-weight: bold;
  margin-bottom: 8px;
}

.outcome-block pre {
  background: #f5f7fa;
  border-radius: 4px;
  padding: 12px;
  font-size: 12px;
  overflow: auto;
  margin: 0;
}
</style>
