import { beforeEach, describe, expect, it, vi } from 'vitest'

const { get, createEventStream } = vi.hoisted(() => ({
  get: vi.fn(),
  createEventStream: vi.fn(() => vi.fn()),
}))
vi.mock('@/api/client', () => ({
  pythonClient: { get },
  buildPythonUrl: (path: string) => `https://python.example/base${path}`,
}))
vi.mock('@/api/sse', () => ({ createEventStream }))

import {
  approveDecision,
  getAdminSecrets,
  getAuditEvents,
  streamAdminApprovals,
} from '@/api/python'

describe('Python Admin API 参数与安全边界', () => {
  beforeEach(() => {
    get.mockReset()
    createEventStream.mockClear()
    vi.unstubAllGlobals()
  })

  it('审计时间范围原样传给后端', async () => {
    get.mockResolvedValue({ data: { events: [] } })
    const params = {
      start_time: '2026-09-19T10:00:00.000Z',
      end_time: '2026-09-19T11:00:00.000Z',
      limit: 100,
    }
    await getAuditEvents(params)
    expect(get).toHaveBeenCalledWith('/v1/admin/audit', { params })
  })

  it('Secret API 仅返回元数据列表', async () => {
    const secrets = [{ ref: 'db/password', tenant_id: null, backend: 'file', has_value: true }]
    get.mockResolvedValue({ data: { secrets } })
    await expect(getAdminSecrets()).resolves.toEqual(secrets)
    expect(get).toHaveBeenCalledWith('/v1/admin/secrets')
  })

  it('审批 SSE 使用已落地管理端点和当前 Python base URL', () => {
    streamAdminApprovals(vi.fn())
    expect(createEventStream).toHaveBeenCalledWith(expect.objectContaining({
      url: 'https://python.example/base/v1/admin/approvals/stream?max_wait=60',
    }))
  })

  it('审批提交使用自定义 Python base URL 且仅携带独立凭证', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ decision_id: 'd1', verdict: 'approve' }),
    })
    vi.stubGlobal('fetch', fetchMock)

    await approveDecision('decision/a', { comment: 'ok' }, 'approval-credential')

    expect(fetchMock).toHaveBeenCalledWith(
      'https://python.example/base/v1/admin/approvals/decision%2Fa/approve',
      {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: 'Bearer approval-credential',
        },
        body: JSON.stringify({ comment: 'ok' }),
      },
    )
  })
})
