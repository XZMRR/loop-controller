import { pythonClient } from './client'

export interface HealthStatus {
  status: string
  opa_reachable: boolean
  gateway_ready: boolean
  evidence_status: string
  anchor_status: string
  persistence: Record<string, object>
  durability: string
  uptime_seconds: number
  harness_backends: Array<Record<string, any>>
}

export interface PendingApproval {
  request_id: string
  decision_id: string
  tool_name: string
  requester_id: string
  reason: string
}

export interface ApprovalActionRequest {
  approver: string
  comment: string
}

export interface ApprovalActionResponse {
  decision_id: string
  verdict: string
}

export interface AuditEvent {
  event_id: string
  trace_id: string
  timestamp: string
  session_id?: string
  task_id?: string
  agent_id?: string
  user_id?: string
  tool_name?: string
  action: string
  verdict?: string
  reason?: string
}

export interface RevokeRequest {
  type: 'agent' | 'user' | 'tool' | 'secret'
  id: string
  reason: string
  expires_at?: string
  tenant_id?: string
}

export interface RevocationEntry {
  type: string
  id: string
  reason: string
  revoked_at: string
  expires_at?: string
}

export interface RevocationListResponse {
  revocations: RevocationEntry[]
  kill_switch: {
    enabled: boolean
    reason?: string
    activated_at?: string
  }
}

export async function getHealth(): Promise<HealthStatus> {
  const { data } = await pythonClient.get('/health')
  return data
}

export interface ApprovalHistoryItem {
  request_id: string
  decision_id: string
  agent_id: string
  tool_name: string
  requester_id: string
  approver_id: string
  reason: string
  status: string
  created_at?: string | null
  decided_at?: string | null
}

export async function getApprovalHistory(params: {
  status?: string
  agent_id?: string
  tool_name?: string
  requester_id?: string
  approver_id?: string
  limit?: number
  offset?: number
}): Promise<{ approvals: ApprovalHistoryItem[]; total: number; limit: number; offset: number }> {
  const { data } = await pythonClient.get('/v1/admin/approvals', { params })
  return data
}

export async function getPendingApprovals(): Promise<PendingApproval[]> {
  const { data } = await pythonClient.get('/v1/admin/approvals/pending')
  return data.approvals || []
}

export async function approveDecision(
  decisionId: string,
  body: ApprovalActionRequest
): Promise<ApprovalActionResponse> {
  const { data } = await pythonClient.post(`/v1/admin/approvals/${decisionId}/approve`, body)
  return data
}

export async function denyDecision(
  decisionId: string,
  body: ApprovalActionRequest
): Promise<ApprovalActionResponse> {
  const { data } = await pythonClient.post(`/v1/admin/approvals/${decisionId}/deny`, body)
  return data
}

export function createApprovalSSE(requestId: string): EventSource {
  return new EventSource(`/api/python/v1/wait-for-approval/sse?request_id=${requestId}&max_wait=300`)
}

// ---------------------------------------------------------------------------
// 内核委托审批对账
// ---------------------------------------------------------------------------

export interface KernelApprovalItem {
  approval_id: string
  request_id: string
  decision_id: string
  initiator_agent_id: string
  target_agent_id: string
  session_id?: string
  status: string
  approver_id?: string
  reason?: string
  allowed_tools?: string[]
  task_id?: string
  expires_at: string
  created_at: string
  updated_at: string
  decided_at?: string
}

export interface KernelApprovalReconciliationItem {
  kernel: KernelApprovalItem
  console_decision_id: string
  console_verdict: string
  reconciled: boolean
}

export async function getKernelApprovals(status?: string): Promise<{
  enabled: boolean
  approvals: KernelApprovalReconciliationItem[]
}> {
  const { data } = await pythonClient.get('/v1/admin/a2a/kernel-approvals', {
    params: status ? { status } : {},
  })
  return data
}

export async function getAuditEvents(params: {
  session_id?: string
  task_id?: string
  agent_id?: string
  tool_name?: string
  verdict?: string
  limit?: number
}): Promise<AuditEvent[]> {
  const { data } = await pythonClient.get('/v1/admin/audit', { params })
  return data.events || []
}

export async function revoke(body: RevokeRequest): Promise<void> {
  await pythonClient.post('/admin/revoke', body)
}

export async function getRevocationList(): Promise<RevocationListResponse> {
  const { data } = await pythonClient.get('/admin/revocation-list')
  return data
}

export async function setKillSwitch(enabled: boolean, reason: string): Promise<void> {
  await pythonClient.post('/admin/kill-switch', { enabled, reason })
}

export async function getHarnessBackends(): Promise<Array<Record<string, any>>> {
  const { data } = await pythonClient.get('/v1/admin/harness/backends')
  return data.backends || []
}

export interface AdminAgent {
  agent_id: string
  name: string
  profile_id: string
  owner_id: string
  owner_name?: string | null
  tenant_id?: string | null
  revoked: boolean
}

export async function getAdminAgents(): Promise<AdminAgent[]> {
  const { data } = await pythonClient.get('/v1/admin/agents')
  return data.agents || []
}

export interface AdminAgentDetail extends AdminAgent {
  description?: string | null
  metadata?: Record<string, any>
}

export async function getAdminAgentDetail(agentId: string): Promise<AdminAgentDetail> {
  const { data } = await pythonClient.get(`/v1/admin/agents/${agentId}`)
  return data
}

export async function getAdminProfiles(): Promise<Array<Record<string, any>>> {
  const { data } = await pythonClient.get('/v1/admin/profiles')
  return data.profiles || []
}

export interface ToolPermissionInput {
  allowed: boolean
  require_approval?: boolean
  allowed_args?: Record<string, string[]>
  denied_args?: Record<string, string[]>
  max_calls_per_task?: number | null
}

export async function updateProfileTools(
  profileId: string,
  tools: Record<string, ToolPermissionInput>,
): Promise<{ profile: Record<string, any>; reloaded: boolean }> {
  const { data } = await pythonClient.put(`/v1/admin/profiles/${profileId}/tools`, { tools })
  return data
}

export async function reloadProfiles(): Promise<Array<Record<string, any>>> {
  const { data } = await pythonClient.post('/v1/admin/profiles/reload')
  return data.profiles || []
}

export interface AdminSession {
  token: string
  token_type: string
  expires_at: string
}

export async function loginAdminSession(apiKey: string): Promise<AdminSession> {
  const { data } = await pythonClient.post('/v1/admin/session/login', { api_key: apiKey })
  return data
}

export async function logoutAdminSession(): Promise<boolean> {
  const { data } = await pythonClient.post('/v1/admin/session/logout')
  return data.revoked === true
}

export interface A2AStatus {
  enabled: boolean
  reachable: boolean
  base_url: string
  local_agent: Record<string, any>
}

export async function getA2AStatus(): Promise<A2AStatus> {
  const { data } = await pythonClient.get('/v1/admin/a2a/status')
  return data
}

export interface A2AAgent {
  agent_id: string
  name: string
  profile_id: string
  owner_name?: string | null
  registered?: boolean | null
}

export async function getA2AAgents(): Promise<{ agents: A2AAgent[]; kernel_reachable: boolean }> {
  const { data } = await pythonClient.get('/v1/admin/a2a/agents')
  return data
}

export async function getA2ATask(taskId: string): Promise<Record<string, any>> {
  const { data } = await pythonClient.get(`/v1/admin/a2a/tasks/${taskId}`)
  return data
}

export async function cancelA2ATask(taskId: string, reason = ''): Promise<Record<string, any>> {
  const { data } = await pythonClient.post(`/v1/admin/a2a/tasks/${taskId}/cancel`, { reason })
  return data
}

const TERMINAL_STATUSES = new Set(['completed', 'failed', 'canceled', 'cancelled', 'rejected'])

/**
 * 订阅任务 SSE 状态流；返回停止函数。收到终态事件自动停止并回调。
 * 使用 fetch 而非 EventSource 以便携带 Authorization 头。
 */
export function streamA2ATask(
  taskId: string,
  onEvent: (event: Record<string, any>) => void,
  onError?: (error: Error) => void,
): () => void {
  const abort = new AbortController()
  const run = async () => {
    try {
      const headers: Record<string, string> = {}
      const sessionToken = localStorage.getItem('lc_session_token')
      const sessionExpires = localStorage.getItem('lc_session_expires')
      const sessionValid =
        !!sessionToken &&
        (!sessionExpires || new Date(sessionExpires).getTime() > Date.now())
      const apiKey = localStorage.getItem('lc_api_key')
      if (sessionValid && sessionToken) headers.Authorization = `Bearer ${sessionToken}`
      else if (apiKey) headers['X-API-Key'] = apiKey
      const resp = await fetch(`/api/python/v1/admin/a2a/tasks/${taskId}/stream`, {
        headers,
        signal: abort.signal,
      })
      if (!resp.ok || !resp.body) throw new Error(`stream failed: HTTP ${resp.status}`)
      const reader = resp.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      // eslint-disable-next-line no-constant-condition
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const frames = buffer.split('\n\n')
        buffer = frames.pop() ?? ''
        for (const frame of frames) {
          const line = frame.split('\n').find((l) => l.startsWith('data: '))
          if (!line) continue
          try {
            const raw = JSON.parse(line.slice(6))
            // Go 内核事件为信封结构：任务快照在 payload 字段中
            const event = raw?.payload && raw?.event_type ? raw.payload : raw
            onEvent(event)
            if (TERMINAL_STATUSES.has(String(event?.status ?? ''))) abort.abort()
          } catch {
            // 忽略无法解析的帧
          }
        }
      }
    } catch (error: any) {
      if (!abort.signal.aborted) onError?.(error instanceof Error ? error : new Error(String(error)))
    }
  }
  void run()
  return () => abort.abort()
}

export interface AdminDelegationResult {
  verdict: string
  allowed: boolean
  reason: string
  decision_id: string
  interaction_id: string
  escalation_target?: string | null
  target_entrypoint?: Record<string, any> | null
  modified_args?: Record<string, any> | null
  dispatch: {
    attempted: boolean
    accepted: boolean
    task_id: string
    reason: string
  }
  approval?: {
    request_id: string
    decision_id: string
    approver_id: string
  } | null
}

export async function createAdminDelegation(payload: {
  source_agent_id: string
  target_agent_id: string
  tool_name: string
  arguments?: Record<string, any>
  risk_level?: string
  session_id?: string
  task_id?: string
  allow_redelegation?: boolean
}): Promise<AdminDelegationResult> {
  const { data } = await pythonClient.post('/v1/admin/a2a/delegations', payload)
  return data
}

export interface AdminIdentityConfig {
  provider: string
  config: Record<string, any>
}

export async function getAdminIdentity(): Promise<AdminIdentityConfig> {
  const { data } = await pythonClient.get('/v1/admin/identity')
  return data
}

export async function getAdminEntrypoints(): Promise<Record<string, any>> {
  const { data } = await pythonClient.get('/v1/admin/entrypoints')
  return data
}

export interface GovernEvaluateRequest {
  agent_id: string
  user_id: string
  tool_name: string
  arguments?: Record<string, any>
  task_context?: string
}

export interface GovernEvaluateResponse {
  verdict: string
  reason: string
  policy_hits: string[]
  risk_level?: string | null
  risk_tags: string[]
  policy_version: string
  profile_version: string
  dry_run: boolean
}

export async function evaluateGovern(body: GovernEvaluateRequest): Promise<GovernEvaluateResponse> {
  const { data } = await pythonClient.post('/v1/admin/govern/evaluate', body)
  return data
}
