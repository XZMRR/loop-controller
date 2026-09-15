<template>
  <div>
    <el-card shadow="hover">
      <template #header>
        <span>A2A 治理</span>
      </template>
      <el-alert
        title="A2A 治理模块为预留扩展，后续对接 Go A2A Kernel"
        type="info"
        :closable="false"
      />
      <p>Go A2A Kernel 地址：{{ auth.a2aUrl }}</p>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
</script>
