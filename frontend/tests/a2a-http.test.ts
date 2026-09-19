import { beforeEach, describe, expect, it, vi } from 'vitest'

const { get } = vi.hoisted(() => ({ get: vi.fn() }))
vi.mock('@/api/client', () => ({ pythonClient: { get } }))

import { HttpA2ATaskDataSource, buildTaskTrees, normalizeTask } from '@/api/a2a/http'
import type { KernelTask } from '@/api/a2a/http'

function task(task_id: string, parent_task_id = '', created_at = '2026-09-19T10:00:00Z'): KernelTask {
  return {
    task_id,
    initiator_agent_id: 'source',
    target_agent_id: 'target',
    status: 'running',
    delegation_depth: parent_task_id ? 1 : 0,
    parent_task_id,
    root_task_id: '',
    allowed_tools: ['echo'],
    allow_redelegation: false,
    budget: { token_count: 10, payment_amount: 0 },
    created_at,
    updated_at: created_at,
  }
}

describe('HttpA2ATaskDataSource', () => {
  beforeEach(() => get.mockReset())

  it('只调用 Python Admin 任务端点并规范化字段', async () => {
    get.mockResolvedValue({ data: { tasks: [task('root')] } })
    const [root] = await new HttpA2ATaskDataSource().listTaskTrees()

    expect(get).toHaveBeenCalledWith('/v1/admin/a2a/tasks')
    expect(root).toMatchObject({
      task_id: 'root',
      depth: 0,
      parent_task_id: null,
      root_task_id: 'root',
      scope: ['echo'],
      deadline: null,
    })
  })

  it('详情 ID 编码后走 Python Admin 并规范化', async () => {
    get.mockResolvedValue({ data: task('task/a') })
    const result = await new HttpA2ATaskDataSource().getTask('task/a')
    expect(get).toHaveBeenCalledWith('/v1/admin/a2a/tasks/task%2Fa')
    expect(result.task_id).toBe('task/a')
  })
})

describe('A2A 任务树适配', () => {
  it('将 Go 扁平字段规范化', () => {
    const result = normalizeTask({ ...task('root'), delegation_depth: 3, allowed_tools: undefined })
    expect(result.depth).toBe(3)
    expect(result.scope).toEqual([])
    expect(result.parent_task_id).toBeNull()
    expect(result.root_task_id).toBe('root')
    expect(result.deadline).toBeNull()
  })

  it('O(n) 组树并按 created_at 稳定排序', () => {
    const trees = buildTaskTrees([
      task('child-b', 'root', '2026-09-19T10:02:00Z'),
      task('root', '', '2026-09-19T10:00:00Z'),
      task('child-a', 'root', '2026-09-19T10:01:00Z'),
      task('same-1', '', '2026-09-19T10:03:00Z'),
      task('same-2', '', '2026-09-19T10:03:00Z'),
    ])
    expect(trees.map((item) => item.task_id)).toEqual(['root', 'same-1', 'same-2'])
    expect(trees[0].children?.map((item) => item.task_id)).toEqual(['child-a', 'child-b'])
  })

  it('orphan 与环节点均提升为根，避免节点丢失或递归环', () => {
    const trees = buildTaskTrees([
      task('orphan', 'missing'),
      task('cycle-a', 'cycle-b'),
      task('cycle-b', 'cycle-a'),
    ])
    expect(trees.map((item) => item.task_id)).toEqual(['orphan', 'cycle-a', 'cycle-b'])
    expect(trees.every((item) => item.parent_task_id === null && !item.children)).toBe(true)
  })
})
