import { describe, expect, it } from 'vitest'
import { MockPolicyDataSource } from '@/api/policy/mock'
import type { PolicyShadowSample } from '@/api/policy'

const OK_SAMPLES: PolicyShadowSample[] = [
  {
    sample_id: 's1',
    package: 'governance.interaction',
    input: { tool_name: 'web_search' },
    redaction_attestation: {},
  },
  {
    sample_id: 's2',
    package: 'governance.interaction',
    input: { tool_name: 'send_email', force_deny: true },
    redaction_attestation: {},
  },
]

describe('MockPolicyDataSource 候选生命周期', () => {
  it('列表覆盖全部六种状态，字段与后端 payload 对齐', async () => {
    const source = new MockPolicyDataSource()
    const candidates = await source.listCandidates()
    const states = new Set(candidates.map((c) => c.state))
    for (const state of ['draft', 'validated', 'published', 'loaded', 'failed']) {
      expect(states.has(state as any)).toBe(true)
    }
    for (const c of candidates) {
      expect(c.candidate_id).toMatch(/^cand-/)
      expect(c.revision).toMatch(/^rev-/)
      expect(c.source_sha256).toMatch(/^sha256:/)
      expect(c.artifact_sha256).toMatch(/^sha256:/)
      expect(typeof c.artifact_size).toBe('number')
      expect(c.created_by).toBeTruthy()
    }
  })

  it('draft 校验通过后转 validated，结果带 stages 与 result_sha256', async () => {
    const source = new MockPolicyDataSource()
    const created = await source.createCandidate({
      files: [{ path: 'interaction.rego', content: 'package governance.interaction' }],
    })
    expect(created.state).toBe('draft')
    const { candidate, validation } = await source.validateCandidate(created.candidate_id)
    expect(candidate.state).toBe('validated')
    expect(validation?.ok).toBe(true)
    expect(validation?.result_sha256).toMatch(/^sha256:/)
    expect(validation?.stages.length).toBeGreaterThan(0)
  })

  it('源内容含 violation 的 draft 校验失败转 failed（422 语义不抛错）', async () => {
    const source = new MockPolicyDataSource()
    // 种子数据 cand-3004 的源内容含 violation 标记
    const { candidate, validation } = await source.validateCandidate('cand-3004')
    expect(candidate.state).toBe('failed')
    expect(validation?.ok).toBe(false)
    expect(validation?.failure_code).toBeTruthy()
  })

  it('非 draft 候选重复校验返回当前状态（对齐后端 200 幂等）', async () => {
    const source = new MockPolicyDataSource()
    const again = await source.validateCandidate('cand-3002')
    expect(again.candidate.state).toBe('published')
  })

  it('创建空 files 返回 invalid_candidate', async () => {
    const source = new MockPolicyDataSource()
    await expect(source.createCandidate({ files: {} })).rejects.toThrow('invalid_candidate')
  })
})

describe('MockPolicyDataSource 发布与回滚', () => {
  it('validated 候选可发布，状态转 published 且 status 指到新 revision', async () => {
    const source = new MockPolicyDataSource()
    const before = await source.getStatus()
    const result = await source.publishCandidate('cand-3003', { base_revision: 'rev-0002' })
    expect(result.state).toBe('published')
    expect(result.revision).toBe('rev-0003')
    expect(result.generation).toBe(before.generation + 1)
    const after = await source.getCandidate('cand-3003')
    expect(after.state).toBe('published')
    expect(after.published_at).toBeTruthy()
    const status = await source.getStatus()
    expect(status.expected_revision).toBe('rev-0003')
  })

  it('draft 候选发布返回 candidate_state_conflict', async () => {
    const source = new MockPolicyDataSource()
    await expect(
      source.publishCandidate('cand-3004', { base_revision: 'rev-0002' }),
    ).rejects.toThrow('candidate_state_conflict')
  })

  it('base_revision 不匹配返回 base_revision_conflict', async () => {
    const source = new MockPolicyDataSource()
    await expect(
      source.publishCandidate('cand-3003', { base_revision: 'rev-9999' }),
    ).rejects.toThrow('base_revision_conflict')
  })

  it('回滚到历史 revision：候选重新置 published 并记录 rollback_of_revision', async () => {
    const source = new MockPolicyDataSource()
    const before = await source.getStatus()
    const result = await source.rollbackPolicy({ revision: 'rev-0001' })
    expect(result.state).toBe('published')
    expect(result.revision).toBe('rev-0001')
    expect(result.generation).toBe(before.generation + 1)
    const candidate = await source.getCandidate(result.candidate_id)
    expect(candidate.rollback_of_revision).toBe(before.expected_revision)
  })

  it('回滚到不存在 revision 返回 invalid_revision', async () => {
    const source = new MockPolicyDataSource()
    await expect(source.rollbackPolicy({ revision: 'rev-9999' })).rejects.toThrow(
      'invalid_revision',
    )
  })
})

describe('MockPolicyDataSource 影子运行', () => {
  it('validated 候选影子运行返回差异统计（force_deny 样本判为 allow→deny）', async () => {
    const source = new MockPolicyDataSource()
    const candidate = await source.getCandidate('cand-3003')
    const result = await source.runShadow(candidate.candidate_id, {
      revision: candidate.revision,
      artifact_sha256: candidate.artifact_sha256,
      samples: OK_SAMPLES,
    })
    expect(result.ok).toBe(true)
    expect(result.total).toBe(2)
    expect(result.same_count).toBe(1)
    expect(result.changed_count).toBe(1)
    expect(result.allow_to_deny).toBe(1)
    expect(result.items.length).toBe(2)
  })

  it('draft 候选影子运行返回 candidate_state_conflict', async () => {
    const source = new MockPolicyDataSource()
    const candidate = await source.getCandidate('cand-3004')
    await expect(
      source.runShadow(candidate.candidate_id, {
        revision: candidate.revision,
        artifact_sha256: candidate.artifact_sha256,
        samples: [],
      }),
    ).rejects.toThrow('candidate_state_conflict')
  })

  it('revision 或 artifact_sha256 不匹配返回 candidate_artifact_mismatch', async () => {
    const source = new MockPolicyDataSource()
    const candidate = await source.getCandidate('cand-3003')
    await expect(
      source.runShadow(candidate.candidate_id, {
        revision: 'rev-9999',
        artifact_sha256: candidate.artifact_sha256,
        samples: [],
      }),
    ).rejects.toThrow('candidate_artifact_mismatch')
  })

  it('样本缺 sample_id 返回 invalid_shadow_samples', async () => {
    const source = new MockPolicyDataSource()
    const candidate = await source.getCandidate('cand-3003')
    await expect(
      source.runShadow(candidate.candidate_id, {
        revision: candidate.revision,
        artifact_sha256: candidate.artifact_sha256,
        samples: [{ package: 'governance.interaction', input: {} } as any],
      }),
    ).rejects.toThrow('invalid_shadow_samples')
  })
})

describe('MockPolicyDataSource 状态与审计', () => {
  it('status 返回 lifecycle 形状（generation 单调递增）', async () => {
    const source = new MockPolicyDataSource()
    const status = await source.getStatus()
    expect(status.expected_revision).toBeTruthy()
    expect(status.loaded_instances).toBe(status.required_instances)
    await source.publishCandidate('cand-3003', {})
    const after = await source.getStatus()
    expect(after.generation).toBe(status.generation + 1)
  })

  it('生命周期操作写审计（倒序返回）', async () => {
    const source = new MockPolicyDataSource()
    const created = await source.createCandidate({
      files: { 'a.rego': 'package a' },
    })
    await source.validateCandidate(created.candidate_id)
    const events = await source.listAudit()
    expect(events.length).toBeGreaterThanOrEqual(2)
    expect(events[0].action).toBe('policy_validate')
    expect(events[1].action).toBe('policy_candidate_create')
    expect(events.every((e) => e.actor === 'console-admin')).toBe(true)
  })

  it('返回对象是深拷贝，外部修改不污染内部数据', async () => {
    const source = new MockPolicyDataSource()
    const list = await source.listCandidates()
    list[0].state = 'tampered'
    const again = await source.listCandidates()
    expect(again[0].state).not.toBe('tampered')
  })
})
