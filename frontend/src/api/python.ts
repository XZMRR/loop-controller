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

export async function getAdminProfiles(): Promise<Array<Record<string, any>>> {
  const { data } = await pythonClient.get('/v1/admin/profiles')
  return data.profiles || []
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
