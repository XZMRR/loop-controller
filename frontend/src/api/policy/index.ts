import type { PolicyDataSource } from './types'
import { HttpPolicyDataSource } from './http'

/**
 * 当前数据源：Http（v0.55 契约冻结后切换，Mock 保留在 ./mock 供测试）。
 * 契约（server.py 3236-3243，handler 1819-2061）：
 *   GET  /v1/admin/policy/candidates                          → { candidates: Candidate[] }
 *   POST /v1/admin/policy/candidates                          ← { files, base_revision? } → 201
 *   GET  /v1/admin/policy/candidates/{id}
 *   POST /v1/admin/policy/candidates/{id}/validate            → { **candidate, validation }（失败 422 同 payload）
 *   POST /v1/admin/policy/candidates/{id}/shadow              ← { revision, artifact_sha256, samples[] }
 *   POST /v1/admin/policy/candidates/{id}/publish             ← { base_revision?, waiver_reason? } → 202
 *   POST /v1/admin/policy/rollback                            ← { revision, base_revision?, waiver_reason? } → 202
 *   GET  /v1/admin/policy/status
 *   GET  /v1/admin/policy/audit                               → { events: [] }
 */
export const policySource: PolicyDataSource = new HttpPolicyDataSource()

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
