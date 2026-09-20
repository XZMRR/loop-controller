import { beforeEach, describe, expect, it, vi } from 'vitest'

const { get, post } = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }))
vi.mock('@/api/client', () => ({ pythonClient: { get, post } }))

import { HttpA2ADeadLetterDataSource } from '@/api/a2a/http'
import type { A2ADeadLetterItem } from '@/api/a2a/types'

const ITEM: A2ADeadLetterItem = {
  assignment_id: 'a/1',
  task_id: 'task-1',
  kind: 'target_execution',
  tenant_id: 'tenant-a',
  initiator: 'agent-a',
  state: 'dead_letter',
  revision: 3,
  replay_count: 0,
  failure_category: 'remote_timeout',
  not_before: null,
  deadline: null,
  outcome: null,
  execution_receipt: null,
  consumed_budget: null,
  created_at: '2026-09-19T10:00:00Z',
  updated_at: '2026-09-19T10:05:00Z',
}

describe('HttpA2ADeadLetterDataSource', () => {
  beforeEach(() => {
    get.mockReset()
    post.mockReset()
  })

  it('列表解包 assignments，缺省空数组', async () => {
    get.mockResolvedValue({ data: { assignments: [ITEM] } })
    expect(await new HttpA2ADeadLetterDataSource().listDeadLetters()).toEqual([ITEM])
    expect(get).toHaveBeenCalledWith('/v1/admin/a2a/dead-letters')

    get.mockResolvedValue({ data: {} })
    expect(await new HttpA2ADeadLetterDataSource().listDeadLetters()).toEqual([])
  })

  it('重放 POST 请求体与路径编码', async () => {
    post.mockResolvedValue({ data: ITEM })
    const params = { tenant_id: 'tenant-a', expected_revision: 3 }
    await new HttpA2ADeadLetterDataSource().replayDeadLetter('a/1', params)
    expect(post).toHaveBeenCalledWith('/v1/admin/a2a/dead-letters/a%2F1/replay', params)
  })

  it('重放可携带 not_before', async () => {
    post.mockResolvedValue({ data: ITEM })
    const params = { tenant_id: 'tenant-a', expected_revision: 3, not_before: '2026-09-20T00:00:00Z' }
    await new HttpA2ADeadLetterDataSource().replayDeadLetter('a/1', params)
    expect(post).toHaveBeenCalledWith('/v1/admin/a2a/dead-letters/a%2F1/replay', params)
  })
})
