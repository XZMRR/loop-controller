// Policy 生命周期 Http 数据源（v0.55 契约冻结后切换）。
// 路由：GET/POST /v1/admin/policy/candidates、GET .../{id}、POST .../{id}/validate、
//       POST .../{id}/shadow、POST .../{id}/publish、POST /v1/admin/policy/rollback、
//       GET /v1/admin/policy/status、GET /v1/admin/policy/audit
// 关键语义（server.py 1763-2061）：
//   validate  非 draft 幂等返回 200（无 validation 字段）；draft 校验 200/422 均带
//             candidate + validation payload → 422 不抛错；
//   shadow    状态门禁 409；revision/artifact 不匹配 409；!ok 时 503 仍带结果 payload
//             → 503 不抛错，由 result.ok 区分；
//   publish   202 {**result, state: 'published'}；rollback 202 且按 revision 找回候选。

import { pythonClient } from '@/api/client'
import type {
  PolicyCandidate,
  PolicyCandidateCreateParams,
  PolicyDataSource,
  PolicyPublishParams,
  PolicyPublishResult,
  PolicyRollbackParams,
  PolicyRollbackResult,
  PolicyShadowRunParams,
  PolicyShadowRunResult,
  PolicyStatus,
  PolicyValidationResult,
} from './types'

/** 从 axios 错误响应中按状态码提取 payload（validate 422 / shadow 503 语义） */
function payloadFromStatus(error: unknown, status: number): any | null {
  const response = (error as { response?: { status?: number; data?: any } })?.response
  return response?.status === status ? (response.data ?? null) : null
}

export class HttpPolicyDataSource implements PolicyDataSource {
  async listCandidates(): Promise<PolicyCandidate[]> {
    const { data } = await pythonClient.get<{ candidates?: PolicyCandidate[] }>(
      '/v1/admin/policy/candidates',
    )
    return data.candidates ?? []
  }

  async getCandidate(candidateId: string): Promise<PolicyCandidate> {
    const { data } = await pythonClient.get<PolicyCandidate>(
      `/v1/admin/policy/candidates/${encodeURIComponent(candidateId)}`,
    )
    return data
  }

  async createCandidate(params: PolicyCandidateCreateParams): Promise<PolicyCandidate> {
    const { data } = await pythonClient.post<PolicyCandidate>('/v1/admin/policy/candidates', {
      files: params.files,
      base_revision: params.base_revision ?? null,
    })
    return data
  }

  async validateCandidate(candidateId: string): Promise<{
    candidate: PolicyCandidate
    validation: PolicyValidationResult | null
  }> {
    const url = `/v1/admin/policy/candidates/${encodeURIComponent(candidateId)}/validate`
    try {
      const { data } = await pythonClient.post<PolicyCandidate & { validation?: PolicyValidationResult }>(url)
      // 非 draft 幂等返回：payload 无 validation 字段
      const { validation, ...candidate } = data
      return { candidate, validation: validation ?? null }
    } catch (error) {
      const payload = payloadFromStatus(error, 422)
      if (payload) {
        const { validation, ...candidate } = payload as PolicyCandidate & {
          validation: PolicyValidationResult
        }
        return { candidate, validation }
      }
      throw error
    }
  }

  async runShadow(
    candidateId: string,
    params: PolicyShadowRunParams,
  ): Promise<PolicyShadowRunResult> {
    const url = `/v1/admin/policy/candidates/${encodeURIComponent(candidateId)}/shadow`
    try {
      const { data } = await pythonClient.post<PolicyShadowRunResult>(url, params)
      return data
    } catch (error) {
      // !ok 时后端 503 仍带结果 payload（对齐 Mock 语义：不抛错，由 ok 区分）
      const payload = payloadFromStatus(error, 503)
      if (payload) return payload
      throw error
    }
  }

  async publishCandidate(
    candidateId: string,
    params: PolicyPublishParams,
  ): Promise<PolicyPublishResult> {
    const { data } = await pythonClient.post<PolicyPublishResult>(
      `/v1/admin/policy/candidates/${encodeURIComponent(candidateId)}/publish`,
      { base_revision: params.base_revision ?? null, waiver_reason: params.waiver_reason ?? null },
    )
    return data
  }

  async rollbackPolicy(params: PolicyRollbackParams): Promise<PolicyRollbackResult> {
    const { data } = await pythonClient.post<PolicyRollbackResult>('/v1/admin/policy/rollback', {
      revision: params.revision,
      base_revision: params.base_revision ?? null,
      waiver_reason: params.waiver_reason ?? null,
    })
    return data
  }

  async getStatus(): Promise<PolicyStatus> {
    const { data } = await pythonClient.get<PolicyStatus>('/v1/admin/policy/status')
    return data
  }

  async listAudit(): Promise<any[]> {
    const { data } = await pythonClient.get<{ events?: any[] }>('/v1/admin/policy/audit')
    return data.events ?? []
  }
}
