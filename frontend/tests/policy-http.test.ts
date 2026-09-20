import { beforeEach, describe, expect, it, vi } from 'vitest'

const { get, post } = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }))
vi.mock('@/api/client', () => ({ pythonClient: { get, post } }))

import { HttpPolicyDataSource } from '@/api/policy/http'
import type { PolicyValidationResult } from '@/api/policy/types'

const CANDIDATE = {
  candidate_id: 'c/1',
  state: 'draft',
  base_revision: null,
  source_sha256: 'src',
  revision: 'r1',
  artifact_sha256: 'art',
  artifact_size: 128,
  created_by: 'admin',
  created_at: '2026-09-19T10:00:00Z',
  published_at: null,
  rollback_of_revision: null,
  tenant_id: null,
}

const VALIDATION: PolicyValidationResult = {
  ok: false,
  opa_version: '1.19.0',
  stages: [],
  package_default_deny: {},
  failure_code: 'opa_nonzero_exit',
  result_sha256: 'sha',
}

const SHADOW_RESULT = {
  ok: false,
  total: 1,
  same_count: 0,
  changed_count: 1,
  allow_to_deny: 1,
  deny_to_allow: 0,
  approval_transitions: 0,
  modify_transitions: 0,
  error_count: 0,
  input_set_digest: 'digest',
  result_sha256: 'sha',
  failure_code: null,
  items: [],
}

function axiosError(status: number, data: any): Error {
  return Object.assign(new Error(`HTTP ${status}`), { response: { status, data } })
}

describe('HttpPolicyDataSource 基础端点', () => {
  beforeEach(() => {
    get.mockReset()
    post.mockReset()
  })

  it('列表/详情/创建/状态/审计', async () => {
    const source = new HttpPolicyDataSource()

    get.mockResolvedValue({ data: { candidates: [CANDIDATE] } })
    expect(await source.listCandidates()).toEqual([CANDIDATE])

    get.mockResolvedValue({ data: CANDIDATE })
    expect(await source.getCandidate('c/1')).toEqual(CANDIDATE)
    expect(get).toHaveBeenCalledWith('/v1/admin/policy/candidates/c%2F1')

    post.mockResolvedValue({ data: CANDIDATE })
    await source.createCandidate({ files: { 'tool.rego': 'package x' }, base_revision: 'r0' })
    expect(post).toHaveBeenCalledWith('/v1/admin/policy/candidates', {
      files: { 'tool.rego': 'package x' },
      base_revision: 'r0',
    })

    get.mockResolvedValue({ data: { state: 'loading' } })
    expect(await source.getStatus()).toEqual({ state: 'loading' })

    get.mockResolvedValue({ data: {} })
    expect(await source.listAudit()).toEqual([])
  })
})

describe('HttpPolicyDataSource 语义化端点', () => {
  beforeEach(() => {
    get.mockReset()
    post.mockReset()
  })

  it('validate 成功拆分 candidate 与 validation', async () => {
    post.mockResolvedValue({ data: { ...CANDIDATE, state: 'validated', validation: VALIDATION } })
    const result = await new HttpPolicyDataSource().validateCandidate('c/1')
    expect(result.candidate.state).toBe('validated')
    expect(result.validation).toEqual(VALIDATION)
    expect(post).toHaveBeenCalledWith('/v1/admin/policy/candidates/c%2F1/validate')
  })

  it('validate 非 draft 幂等返回时 validation 为 null', async () => {
    post.mockResolvedValue({ data: { ...CANDIDATE, state: 'validated' } })
    const result = await new HttpPolicyDataSource().validateCandidate('c/1')
    expect(result.candidate.state).toBe('validated')
    expect(result.validation).toBeNull()
  })

  it('validate 422 不抛错并返回失败 validation', async () => {
    post.mockRejectedValue(axiosError(422, { ...CANDIDATE, state: 'failed', validation: VALIDATION }))
    const result = await new HttpPolicyDataSource().validateCandidate('c/1')
    expect(result.candidate.state).toBe('failed')
    expect(result.validation?.ok).toBe(false)
  })

  it('validate 其他错误照常抛出', async () => {
    post.mockRejectedValue(axiosError(409, { error: 'candidate_state_conflict' }))
    await expect(new HttpPolicyDataSource().validateCandidate('c/1')).rejects.toThrow('HTTP 409')
  })

  it('shadow 成功返回结果', async () => {
    post.mockResolvedValue({ data: { ...SHADOW_RESULT, ok: true } })
    const params = { revision: 'r1', artifact_sha256: 'art', samples: [] }
    const result = await new HttpPolicyDataSource().runShadow('c/1', params)
    expect(result.ok).toBe(true)
    expect(post).toHaveBeenCalledWith('/v1/admin/policy/candidates/c%2F1/shadow', params)
  })

  it('shadow 503 不抛错并返回 !ok 结果', async () => {
    post.mockRejectedValue(axiosError(503, SHADOW_RESULT))
    const result = await new HttpPolicyDataSource().runShadow('c/1', {
      revision: 'r1',
      artifact_sha256: 'art',
      samples: [],
    })
    expect(result.ok).toBe(false)
    expect(result.changed_count).toBe(1)
  })

  it('shadow 非 503 错误照常抛出', async () => {
    post.mockRejectedValue(axiosError(409, { error: 'candidate_artifact_mismatch' }))
    await expect(
      new HttpPolicyDataSource().runShadow('c/1', { revision: 'r1', artifact_sha256: 'art', samples: [] }),
    ).rejects.toThrow('HTTP 409')
  })

  it('publish 202 负载与请求体（base_revision/waiver_reason 缺省 null）', async () => {
    post.mockResolvedValue({ data: { revision: 'r2', generation: 3, state: 'published' } })
    const result = await new HttpPolicyDataSource().publishCandidate('c/1', { base_revision: 'r1' })
    expect(result).toEqual({ revision: 'r2', generation: 3, state: 'published' })
    expect(post).toHaveBeenCalledWith('/v1/admin/policy/candidates/c%2F1/publish', {
      base_revision: 'r1',
      waiver_reason: null,
    })
  })

  it('rollback 走独立端点且负载含 revision', async () => {
    post.mockResolvedValue({ data: { candidate_id: 'c/0', revision: 'r0', generation: 4, state: 'published' } })
    const result = await new HttpPolicyDataSource().rollbackPolicy({ revision: 'r0' })
    expect(result.revision).toBe('r0')
    expect(post).toHaveBeenCalledWith('/v1/admin/policy/rollback', {
      revision: 'r0',
      base_revision: null,
      waiver_reason: null,
    })
  })
})
