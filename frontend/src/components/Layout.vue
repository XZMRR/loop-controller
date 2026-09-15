<template>
  <el-container class="layout-container">
    <el-aside width="220px" class="sidebar">
      <div class="logo">
        <el-icon size="24"><Monitor /></el-icon>
        <span>Loop Controller</span>
      </div>
      <el-menu
        :default-active="$route.path"
        router
        class="sidebar-menu"
        background-color="#304156"
        text-color="#bfcbd9"
        active-text-color="#409eff"
      >
        <el-menu-item index="/dashboard">
          <el-icon><Odometer /></el-icon>
          <span>仪表盘</span>
        </el-menu-item>
        <el-menu-item index="/approvals">
          <el-icon><Checked /></el-icon>
          <span>审批台</span>
        </el-menu-item>
        <el-menu-item index="/agents">
          <el-icon><User /></el-icon>
          <span>Agent 管理</span>
        </el-menu-item>
        <el-menu-item index="/tools">
          <el-icon><Tools /></el-icon>
          <span>工具策略</span>
        </el-menu-item>
        <el-menu-item index="/debug">
          <el-icon><Aim /></el-icon>
          <span>判定调试</span>
        </el-menu-item>
        <el-menu-item index="/audit">
          <el-icon><DocumentCopy /></el-icon>
          <span>审计查询</span>
        </el-menu-item>
        <el-menu-item index="/settings">
          <el-icon><Setting /></el-icon>
          <span>系统配置</span>
        </el-menu-item>
        <el-menu-item index="/a2a">
          <el-icon><Connection /></el-icon>
          <span>A2A 治理</span>
        </el-menu-item>
      </el-menu>
    </el-aside>
    <el-container>
      <el-header class="header">
        <div class="page-title">{{ $route.meta.title || 'Loop Controller' }}</div>
        <div class="header-actions">
          <el-button type="danger" size="small" plain @click="handleLogout">
            退出
          </el-button>
        </div>
      </el-header>
      <el-main class="main-content">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup lang="ts">
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

const router = useRouter()
const auth = useAuthStore()

function handleLogout() {
  auth.logout()
  router.push('/login')
}
</script>

<style scoped>
.layout-container {
  height: 100vh;
}

.sidebar {
  background-color: #304156;
  color: #fff;
}

.logo {
  height: 60px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  font-size: 18px;
  font-weight: bold;
  border-bottom: 1px solid #1f2d3d;
}

.sidebar-menu {
  border-right: none;
}

.header {
  background-color: #fff;
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.08);
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.page-title {
  font-size: 18px;
  font-weight: 500;
}

.main-content {
  background-color: #f5f7fa;
  padding: 20px;
  overflow-y: auto;
}
</style>
