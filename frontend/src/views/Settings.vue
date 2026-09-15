<template>
  <div>
    <el-row :gutter="16">
      <el-col :span="12">
        <el-card shadow="hover">
          <template #header>
            <span>入口配置 (entrypoints.yaml)</span>
          </template>
          <pre>{{ JSON.stringify(entrypoints, null, 2) }}</pre>
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card shadow="hover">
          <template #header>
            <span>身份认证 (identity.yaml)</span>
          </template>
          <pre>{{ JSON.stringify(identity, null, 2) }}</pre>
        </el-card>
      </el-col>
    </el-row>
    <el-card class="mt-4" shadow="hover">
      <template #header>
        <div class="card-header">
          <span>Kill Switch</span>
          <el-tag :type="killSwitchActive ? 'danger' : 'info'">
            {{ killSwitchActive ? '已触发' : '未触发' }}
          </el-tag>
        </div>
      </template>
      <el-form label-position="top" inline>
        <el-form-item label="原因">
          <el-input v-model="killSwitchReason" placeholder="触发 Kill Switch 的原因" />
        </el-form-item>
        <el-form-item>
          <el-button
            :type="killSwitchActive ? 'warning' : 'danger'"
            :loading="killSwitchLoading"
            @click="handleKillSwitch(!killSwitchActive)"
          >
            {{ killSwitchActive ? '解除 Kill Switch' : '触发 Kill Switch' }}
          </el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <el-card class="mt-4" shadow="hover">
      <template #header>
        <span>吊销操作</span>
      </template>
      <el-form :model="revokeForm" label-position="top" inline>
        <el-form-item label="类型">
          <el-select v-model="revokeForm.type" placeholder="选择类型">
            <el-option label="Agent" value="agent" />
            <el-option label="User" value="user" />
            <el-option label="Tool" value="tool" />
            <el-option label="Secret" value="secret" />
          </el-select>
        </el-form-item>
        <el-form-item label="ID">
          <el-input v-model="revokeForm.id" placeholder="要吊销的 ID" />
        </el-form-item>
        <el-form-item label="原因">
          <el-input v-model="revokeForm.reason" placeholder="吊销原因" />
        </el-form-item>
        <el-form-item>
          <el-button type="danger" @click="handleRevoke" :loading="revoking">吊销</el-button>
        </el-form-item>
      </el-form>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { loadEntrypointsConfig, loadIdentityConfig } from '@/api/config'
import { revoke, setKillSwitch, getRevocationList } from '@/api/python'

const entrypoints = ref<any>(null)
const identity = ref<any>(null)
const revoking = ref(false)
const revokeForm = reactive({
  type: 'agent' as const,
  id: '',
  reason: '',
})
const killSwitchActive = ref(false)
const killSwitchReason = ref('')
const killSwitchLoading = ref(false)

async function loadData() {
  try {
    const [ep, id, revocation] = await Promise.all([
      loadEntrypointsConfig(),
      loadIdentityConfig(),
      getRevocationList(),
    ])
    entrypoints.value = ep
    identity.value = id
    killSwitchActive.value = revocation.kill_switch?.enabled || false
  } catch (error: any) {
    ElMessage.error(error.message || '加载配置失败')
  }
}

async function handleKillSwitch(active: boolean) {
  if (active && !killSwitchReason.value.trim()) {
    ElMessage.warning('触发 Kill Switch 必须填写原因')
    return
  }
  killSwitchLoading.value = true
  try {
    await setKillSwitch(active, killSwitchReason.value || 'manual release')
    killSwitchActive.value = active
    ElMessage.success(active ? 'Kill Switch 已触发' : 'Kill Switch 已解除')
  } catch (error: any) {
    ElMessage.error(error.message || '操作失败')
  } finally {
    killSwitchLoading.value = false
  }
}

async function handleRevoke() {
  if (!revokeForm.id.trim() || !revokeForm.reason.trim()) {
    ElMessage.warning('请填写完整')
    return
  }
  revoking.value = true
  try {
    await revoke({
      type: revokeForm.type,
      id: revokeForm.id,
      reason: revokeForm.reason,
    })
    ElMessage.success('吊销成功')
    revokeForm.id = ''
    revokeForm.reason = ''
  } catch (error: any) {
    ElMessage.error(error.message || '吊销失败')
  } finally {
    revoking.value = false
  }
}

onMounted(loadData)
</script>

<style scoped>
.mt-4 {
  margin-top: 16px;
}
</style>
