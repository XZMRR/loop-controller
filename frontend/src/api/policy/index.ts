import type { PolicyDataSource } from './types'
import { MockPolicyDataSource } from './mock'

/**
 * 当前数据源：Mock（演示）。
 * 契约已由 Python runtime 定义：
 *   GET  /v1/admin/policy/candidates                          → { candidates: Candidate[] }
 *   POST /v1/admin/policy/candidates                          ← { files, base_revision? } → 201
 *   GET  /v1/admin/policy/candidates/{id}
 *   POST /v1/admin/policy/candidates/{id}/validate            → { **candidate, validation }（失败 422 同 payload）
 *   POST /v1/admin/policy/candidates/{id}/shadow              ← { revision, artifact_sha256, samples[] }
 *   POST /v1/admin/policy/candidates/{id}/publish             ← { base_revision?, waiver_reason? } → 202
 *   POST /v1/admin/policy/rollback                            ← { revision, base_revision?, waiver_reason? } → 202
 *   GET  /v1/admin/policy/status
 *   GET  /v1/admin/policy/audit                               → { events: [] }
 * Http 实现直接走 pythonClient（/api/python）；视图层（Policies.vue）无需任何改动。
 */
export const policySource: PolicyDataSource = new MockPolicyDataSource()

export type {
  PolicyAuditEvent,
  PolicyCandidate,
  PolicyCandidateCreateParams,
  PolicyCandidateState,
  PolicyDataSource,
  PolicyPublishParams,
  PolicyPublishResult,
  PolicyRollbackParams,
  PolicyRollbackResult,
  PolicyShadowItemResult,
  PolicyShadowRunParams,
  PolicyShadowRunResult,
  PolicyShadowSample,
  PolicyStatus,
  PolicyValidationResult,
  PolicyValidationStage,
} from './types'
