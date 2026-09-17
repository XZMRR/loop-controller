// Policy 生命周期契约层。
// 字段形状以 Python runtime server.py 为准：
//   路由 server.py:3012-3019；candidate payload server.py:1683-1697；
//   validate handler server.py:1763-1792（CandidateValidationResult 见 policy_validation.py:48-56）；
//   shadow handler server.py:1794-1837（ShadowRunResult 见 policy_shadow.py:66-81）；
//   publish/rollback handler server.py:1839-1926；status 形状见 policy_lifecycle.py:227-259。
// 数据 100% 在 Python 侧（policy_delivery 存储 + policy_lifecycle），与 Go A2A 内核无关；
// Http 实现应走 pythonClient（/api/python）。

/** 后端 CandidateState 枚举（infra/policy_delivery.py:60-66） */
export type PolicyCandidateState =
  | 'draft'
  | 'validated'
  | 'published'
  | 'loaded'
  | 'failed'
  | 'superseded'

export interface PolicyCandidate {
  candidate_id: string
  state: PolicyCandidateState
  base_revision: string | null
  source_sha256: string
  revision: string
  artifact_sha256: string
  artifact_size: number
  created_by: string
  created_at: string
  published_at: string | null
  rollback_of_revision: string | null
  tenant_id: string | null
}

/** POST /v1/admin/policy/candidates 请求体；files 可为 path/content 数组或 map */
export interface PolicyCandidateCreateParams {
  files: { path: string; content: string }[] | Record<string, string>
  base_revision?: string | null
}

/** policy_validation.py OPACommandResult */
export interface PolicyValidationStage {
  stage: string
  ok: boolean
  exit_code: number | null
  elapsed_ms: number
  output_summary: string
  failure_code: string | null
}

/** policy_validation.py CandidateValidationResult；校验失败时 HTTP 422 但带此 payload */
export interface PolicyValidationResult {
  ok: boolean
  opa_version: string | null
  stages: PolicyValidationStage[]
  package_default_deny: Record<string, boolean>
  failure_code: string | null
  result_sha256: string
}

/** policy_shadow.py ShadowSample */
export interface PolicyShadowSample {
  sample_id: string
  package: string
  input: Record<string, any>
  expected?: any
  redaction_attestation: Record<string, string>
}

export interface PolicyShadowItemResult {
  sample_id: string
  same: boolean
  baseline_verdict: string
  candidate_verdict: string
  baseline_result_sha256: string
  candidate_result_sha256: string
}

/** policy_shadow.py ShadowRunResult；运行失败时 HTTP 503 但带此 payload */
export interface PolicyShadowRunResult {
  ok: boolean
  total: number
  same_count: number
  changed_count: number
  allow_to_deny: number
  deny_to_allow: number
  approval_transitions: number
  modify_transitions: number
  error_count: number
  input_set_digest: string
  result_sha256: string
  failure_code: string | null
  items: PolicyShadowItemResult[]
}

/** POST .../shadow 请求体；revision + artifact_sha256 必须与候选一致（否则 409 artifact_mismatch） */
export interface PolicyShadowRunParams {
  revision: string
  artifact_sha256: string
  samples: PolicyShadowSample[]
}

/** POST .../publish 请求体；waiver_reason 仅在职责分离被免除时生效 */
export interface PolicyPublishParams {
  base_revision?: string | null
  waiver_reason?: string | null
}

/** publish 响应（202）：{**result, state: "published"}，result 至少含 revision/generation */
export interface PolicyPublishResult {
  revision: string
  generation: number
  state: 'published'
  [key: string]: any
}

/** POST /v1/admin/policy/rollback 请求体 */
export interface PolicyRollbackParams {
  revision: string
  base_revision?: string | null
  waiver_reason?: string | null
}

export interface PolicyRollbackResult {
  candidate_id: string
  revision: string
  generation: number
  state: 'published'
  [key: string]: any
}

/** policy_lifecycle.py status() 返回形状（instances 为实例状态行 map，字段以后端为准） */
export interface PolicyStatus {
  expected_revision: string | null
  active_revision: string | null
  state: string
  generation: number
  required_instances: number
  fresh_instances: number
  loaded_instances: number
  stale_instances: string[]
  error_instances: string[]
  status_ttl_seconds: number
  instances: Record<string, any>
}

/** policy/audit 事件；后端行结构以后端 store 为准，此处只约束展示用公共字段 */
export interface PolicyAuditEvent {
  action: string
  target?: string | null
  actor?: string | null
  created_at?: string
  [key: string]: any
}

export interface PolicyDataSource {
  listCandidates(): Promise<PolicyCandidate[]>
  getCandidate(candidateId: string): Promise<PolicyCandidate>
  createCandidate(params: PolicyCandidateCreateParams): Promise<PolicyCandidate>
  /** draft → validated（ok）或 failed（!ok，不抛错，由 validation.ok 区分）；
   *  非 draft 状态返回当前候选（对齐后端 200 幂等语义） */
  validateCandidate(candidateId: string): Promise<{
    candidate: PolicyCandidate
    validation: PolicyValidationResult | null
  }>
  /** 仅 validated/published/loaded 可运行（否则 409 candidate_state_conflict） */
  runShadow(
    candidateId: string,
    params: PolicyShadowRunParams,
  ): Promise<PolicyShadowRunResult>
  /** 仅 validated 可发布（否则 409）；base_revision 不匹配时 409 base_revision_conflict */
  publishCandidate(
    candidateId: string,
    params: PolicyPublishParams,
  ): Promise<PolicyPublishResult>
  /** 回滚到历史 revision；revision 不存在时 404 invalid_revision */
  rollbackPolicy(params: PolicyRollbackParams): Promise<PolicyRollbackResult>
  getStatus(): Promise<PolicyStatus>
  listAudit(): Promise<PolicyAuditEvent[]>
}
