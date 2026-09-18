<template>
  <div class="error-state" role="alert">
    <el-alert type="error" :closable="false" show-icon>
      <template #title>
        <span>{{ title }}</span>
      </template>
      <div class="error-body">
        <span class="error-message">{{ message }}</span>
        <el-button
          v-if="!hideRetry"
          size="small"
          text
          type="primary"
          @click="$emit('retry')"
        >
          重试
        </el-button>
      </div>
    </el-alert>
  </div>
</template>

<script setup lang="ts">
/**
 * 数据加载失败的持久错误态（全站统一三态规范的 Error 态）。
 * 约定：
 * - 列表/卡片数据区加载失败用 ErrorState 持久展示（含重试），不弹 toast；
 * - 操作类反馈（保存、审批、重放等）仍用 ElMessage；
 * - Loading 态由 v-loading / el-skeleton 承担，Empty 态由 el-empty 承担。
 */
withDefaults(
  defineProps<{
    message: string
    title?: string
    hideRetry?: boolean
  }>(),
  {
    title: '数据加载失败',
    hideRetry: false,
  },
)

defineEmits<{ (e: 'retry'): void }>()
</script>

<style scoped>
.error-state {
  margin-bottom: 16px;
}

.error-body {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.error-message {
  word-break: break-all;
}
</style>
