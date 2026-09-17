import type {
  PolicyAuditEvent,
  PolicyCandidate,
  PolicyCandidateCreateParams,
  PolicyCandidateState,
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

/**
 * 演示数据源：模拟 Policy 生命周期（draft → validated → published/loaded，
 * 支持 shadow 对比与按 revision 回滚）。语义对齐 Python runtime：
 * - validate 仅 draft 可执行；失败置 failed 并返回 !ok 结果（HTTP 422 语义，不抛错）；
 * - shadow 仅 validated/published/loaded 可运行，revision+artifact_sha256 必须匹配候选；
 * - publish 仅 validated 可发布；rollback 按历史 revision 找回候选并重新置 published；
 * - 状态/审计由 mock 维护，generation 单调递增。
 */

const CANDIDATES: PolicyCandidate[] = [
  {
    candidate_id: 'cand-3001',
    state: 'loaded',
    base_revision: null,
    source_sha256: 'sha256:a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f60001',
    revision: 'rev-0001',
    artifact_sha256: 'sha256:artf0001aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    artifact_size: 4096,
    created_by: 'zhang.wei',
    created_at: '2026-09-08T02:00:00Z',
    published_at: '2026-09-08T02:30:00Z',
    rollback_of_revision: null,
    tenant_id: null,
  },
  {
    candidate_id: 'cand-3002',
    state: 'published',
    base_revision: 'rev-0001',
    source_sha256: 'sha256:a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f60002',
    revision: 'rev-0002',
    artifact_sha256: 'sha256:artf0002bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
    artifact_size: 4120,
    created_by: 'li.na',
    created_at: '2026-09-12T06:10:00Z',
    published_at: '2026-09-12T07:00:00Z',
    rollback_of_revision: null,
    tenant_id: 'tenant-a',
  },
  {
    candidate_id: 'cand-3003',
    state: 'validated',
    base_revision: 'rev-0002',
    source_sha256: 'sha256:a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f60003',
    revision: 'rev-0003',
    artifact_sha256: 'sha256:artf0003cccccccccccccccccccccccccccccccccccccccccccccccccccccc',
    artifact_size: 4188,
    created_by: 'dev-svc-research',
    created_at: '2026-09-16T09:20:00Z',
    published_at: null,
    rollback_of_revision: null,
    tenant_id: 'tenant-a',
  },
  {
    candidate_id: 'cand-3004',
    state: 'draft',
    base_revision: 'rev-0002',
    source_sha256: 'sha256:a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f60004',
    revision: 'rev-0004',
    artifact_sha256: 'sha256:artf0004dddddddddddddddddddddddddddddddddddddddddddddddddddddd',
    artifact_size: 4210,
    created_by: 'release-svc',
    created_at: '2026-09-17T01:05:00Z',
    published_at: null,
    rollback_of_revision: null,
    tenant_id: 'tenant-b',
  },
  {
    candidate_id: 'cand-3005',
    state: 'failed',
    base_revision: 'rev-0002',
    source_sha256: 'sha256:a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f60005',
    revision: 'rev-0005',
    artifact_sha256: 'sha256:artf0005eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',
    artifact_size: 4199,
    created_by: 'dev-svc-research',
    created_at: '2026-09-17T02:40:00Z',
    published_at: null,
    rollback_of_revision: null,
    tenant_id: 'tenant-a',
  },
]

const STATUS: PolicyStatus = {
  expected_revision: 'rev-0002',
  active_revision: 'rev-0002',
  state: 'published',
  generation: 7,
  required_instances: 3,
  fresh_instances: 3,
  loaded_instances: 3,
  stale_instances: [],
  error_instances: [],
  status_ttl_seconds: 300,
  instances: {
    'opa-0': { instance_id: 'opa-0', revision: 'rev-0002', state: 'loaded', error_code: null },
    'opa-1': { instance_id: 'opa-1', revision: 'rev-0002', state: 'loaded', error_code: null },
    'opa-2': { instance_id: 'opa-2', revision: 'rev-0002', state: 'loaded', error_code: null },
  },
}

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T
}

function fakeSha(seed: string): string {
  let hash = 0
  for (let i = 0; i < seed.length; i++) {
    hash = (hash * 31 + seed.charCodeAt(i)) >>> 0
  }
  return `sha256:${hash.toString(16).padStart(8, '0')}${'0'.repeat(56)}`
}

let candSeq = 3006

export class MockPolicyDataSource implements PolicyDataSource {
  private items: PolicyCandidate[] = clone(CANDIDATES)
  private status: PolicyStatus = clone(STATUS)
  private audit: PolicyAuditEvent[] = []
  private validations = new Map<string, PolicyValidationResult>()
  // 候选源文件（candidate_id → path/content），validate 按内容判定
  private sources = new Map<string, Record<string, string>>([
    ['cand-3004', { 'interaction.rego': 'package governance.interaction\n# violation: deny_all 草稿' }],
  ])

  async listCandidates(): Promise<PolicyCandidate[]> {
    return clone(this.items)
  }

  async getCandidate(candidateId: string): Promise<PolicyCandidate> {
    const found = this.items.find((item) => item.candidate_id === candidateId)
    if (!found) throw new Error(`not_found：候选不存在 ${candidateId}`)
    return clone(found)
  }

  async createCandidate(params: PolicyCandidateCreateParams): Promise<PolicyCandidate> {
    const filesMap =
      Array.isArray(params.files)
        ? Object.fromEntries(params.files.map((f) => [f.path, f.content]))
        : params.files
    if (!filesMap || Object.keys(filesMap).length === 0) {
      throw new Error('invalid_candidate：files 不能为空')
    }
    const revision = `rev-${String(candSeq).padStart(4, '0')}`
    const candidate: PolicyCandidate = {
      candidate_id: `cand-${candSeq++}`,
      state: 'draft',
      base_revision: params.base_revision ?? null,
      source_sha256: fakeSha(JSON.stringify(filesMap)),
      revision,
      artifact_sha256: fakeSha(`artifact:${revision}`),
      artifact_size: JSON.stringify(filesMap).length,
      created_by: 'console-admin',
      created_at: new Date().toISOString(),
      published_at: null,
      rollback_of_revision: null,
      tenant_id: null,
    }
    this.items.push(candidate)
    this.sources.set(candidate.candidate_id, filesMap)
    this.recordAudit('policy_candidate_create', candidate.candidate_id, {
      base_revision: candidate.base_revision,
      revision: candidate.revision,
    })
    return clone(candidate)
  }

  async validateCandidate(candidateId: string): Promise<{
    candidate: PolicyCandidate
    validation: PolicyValidationResult | null
  }> {
    const candidate = this.items.find((item) => item.candidate_id === candidateId)
    if (!candidate) throw new Error(`not_found：候选不存在 ${candidateId}`)
    // 对齐后端：非 draft 直接返回当前候选（200 幂等）
    if (candidate.state !== 'draft') {
      return { candidate: clone(candidate), validation: this.validations.get(candidateId) ?? null }
    }
    // mock 判定规则：源内容含 "violation" 即视为校验失败
    const sourceText = JSON.stringify(this.sources.get(candidateId) ?? {})
    const ok = !sourceText.includes('violation')
    const validation: PolicyValidationResult = {
      ok,
      opa_version: '1.19.0',
      stages: [
        {
          stage: 'parse',
          ok: true,
          exit_code: 0,
          elapsed_ms: 12,
          output_summary: 'rego 解析通过',
          failure_code: null,
        },
        {
          stage: 'compile',
          ok: true,
          exit_code: 0,
          elapsed_ms: 48,
          output_summary: '编译通过，2 个包',
          failure_code: null,
        },
        {
          stage: 'eval-smoke',
          ok,
          exit_code: ok ? 0 : 1,
          elapsed_ms: 96,
          output_summary: ok ? '冒烟用例全部通过' : '冒烟用例存在违规放行',
          failure_code: ok ? null : 'package_default_deny_violation',
        },
      ],
      package_default_deny: { 'governance.interaction': true, 'governance.tools': true },
      failure_code: ok ? null : 'package_default_deny_violation',
      result_sha256: fakeSha(`validation:${candidateId}:${ok}`),
    }
    candidate.state = ok ? 'validated' : 'failed'
    this.validations.set(candidateId, validation)
    this.recordAudit('policy_validate', candidateId, {
      revision: candidate.revision,
      result: ok ? 'success' : 'failed',
    })
    return { candidate: clone(candidate), validation }
  }

  async runShadow(
    candidateId: string,
    params: PolicyShadowRunParams,
  ): Promise<PolicyShadowRunResult> {
    const candidate = this.items.find((item) => item.candidate_id === candidateId)
    if (!candidate) throw new Error(`not_found：候选不存在 ${candidateId}`)
    if (!['validated', 'published', 'loaded'].includes(candidate.state)) {
      throw new Error('candidate_state_conflict：仅 validated/published/loaded 可影子运行')
    }
    if (
      params.revision !== candidate.revision ||
      params.artifact_sha256 !== candidate.artifact_sha256
    ) {
      throw new Error('candidate_artifact_mismatch：revision 或 artifact_sha256 与候选不一致')
    }
    for (const sample of params.samples) {
      if (!sample.sample_id || !sample.package) {
        throw new Error('invalid_shadow_samples：sample 缺少 sample_id 或 package')
      }
    }
    // mock 对比规则：input 含 "force_deny": true 的样本判为 allow→deny 变化
    const items = params.samples.map((sample) => {
      const changed = sample.input?.force_deny === true
      const candidateVerdict = changed ? 'deny' : 'allow'
      return {
        sample_id: sample.sample_id,
        same: !changed,
        baseline_verdict: 'allow',
        candidate_verdict: candidateVerdict,
        baseline_result_sha256: fakeSha(`baseline:${sample.sample_id}`),
        candidate_result_sha256: fakeSha(`candidate:${sample.sample_id}:${candidateVerdict}`),
      }
    })
    const changedCount = items.filter((item) => !item.same).length
    const allowToDeny = items.filter(
      (item) => item.baseline_verdict === 'allow' && item.candidate_verdict === 'deny',
    ).length
    const denyToAllow = items.filter(
      (item) => item.baseline_verdict === 'deny' && item.candidate_verdict === 'allow',
    ).length
    const result: PolicyShadowRunResult = {
      ok: true,
      total: items.length,
      same_count: items.length - changedCount,
      changed_count: changedCount,
      allow_to_deny: allowToDeny,
      deny_to_allow: denyToAllow,
      approval_transitions: 0,
      modify_transitions: 0,
      error_count: 0,
      input_set_digest: fakeSha(JSON.stringify(params.samples)),
      result_sha256: fakeSha(`shadow:${candidateId}:${items.length}:${changedCount}`),
      failure_code: null,
      items,
    }
    this.recordAudit('policy_shadow', candidateId, {
      revision: candidate.revision,
      total: result.total,
      changed_count: result.changed_count,
    })
    return clone(result)
  }

  async publishCandidate(
    candidateId: string,
    params: PolicyPublishParams,
  ): Promise<PolicyPublishResult> {
    const candidate = this.items.find((item) => item.candidate_id === candidateId)
    if (!candidate) throw new Error(`not_found：候选不存在 ${candidateId}`)
    if (candidate.state !== 'validated') {
      throw new Error('candidate_state_conflict：仅 validated 候选可发布')
    }
    if (
      params.base_revision != null &&
      candidate.base_revision !== null &&
      params.base_revision !== candidate.base_revision
    ) {
      throw new Error('base_revision_conflict：base_revision 与候选不一致，请刷新后重试')
    }
    candidate.state = 'published'
    candidate.published_at = new Date().toISOString()
    this.status.expected_revision = candidate.revision
    this.status.active_revision = null
    this.status.state = 'published'
    this.status.generation += 1
    this.recordAudit('policy_publish', candidateId, {
      revision: candidate.revision,
      generation: this.status.generation,
    })
    return {
      revision: candidate.revision,
      generation: this.status.generation,
      state: 'published',
    }
  }

  async rollbackPolicy(params: PolicyRollbackParams): Promise<PolicyRollbackResult> {
    const candidate = this.items.find((item) => item.revision === params.revision)
    if (!candidate) throw new Error(`invalid_revision：revision 不存在 ${params.revision}`)
    candidate.state = 'published'
    candidate.rollback_of_revision = this.status.expected_revision
    candidate.published_at = new Date().toISOString()
    this.status.expected_revision = candidate.revision
    this.status.active_revision = null
    this.status.state = 'published'
    this.status.generation += 1
    this.recordAudit('policy_rollback', candidate.candidate_id, {
      revision: candidate.revision,
      generation: this.status.generation,
    })
    return {
      candidate_id: candidate.candidate_id,
      revision: candidate.revision,
      generation: this.status.generation,
      state: 'published',
    }
  }

  async getStatus(): Promise<PolicyStatus> {
    return clone(this.status)
  }

  async listAudit(): Promise<PolicyAuditEvent[]> {
    return clone(this.audit).reverse()
  }

  private recordAudit(action: string, target: string, metadata: Record<string, any>) {
    this.audit.push({
      action,
      target,
      actor: 'console-admin',
      created_at: new Date().toISOString(),
      metadata,
    })
  }
}

export type { PolicyCandidateState }
