import { describe, it, expect } from 'vitest'
import { MockA2ATaskDataSource } from '@/api/a2a/mock'

describe('MockA2ATaskDataSource', () => {
  const source = new MockA2ATaskDataSource()

  it('返回两跳委托链路：根任务 + 一个子任务', async () => {
    const trees = await source.listTaskTrees()
    expect(trees).toHaveLength(1)
    const root = trees[0]
    expect(root.depth).toBe(0)
    expect(root.parent_task_id).toBeNull()
    expect(root.allow_redelegation).toBe(true)
    expect(root.children).toHaveLength(1)

    const child = root.children![0]
    expect(child.depth).toBe(1)
    expect(child.parent_task_id).toBe(root.task_id)
    expect(child.root_task_id).toBe(root.task_id)
    expect(child.allow_redelegation).toBe(false)
  })

  it('子任务体现防扩大语义：scope 收窄、预算不扩大', async () => {
    const [root] = await source.listTaskTrees()
    const child = root.children![0]
    expect(child.scope).toEqual(['calculate_checksum'])
    expect(child.budget.token_count).toBeLessThanOrEqual(root.budget.token_count)
  })

  it('getTask 按 ID 查找任意节点，未知 ID 抛错', async () => {
    const [root] = await source.listTaskTrees()
    const byRoot = await source.getTask(root.task_id)
    expect(byRoot.task_id).toBe(root.task_id)
    const byChild = await source.getTask(root.children![0].task_id)
    expect(byChild.depth).toBe(1)
    await expect(source.getTask('task-not-exist')).rejects.toThrow('任务不存在')
  })

  it('返回深拷贝：外部修改不影响内部数据', async () => {
    const trees = await source.listTaskTrees()
    trees[0].status = 'tampered'
    trees[0].children![0].consumed_budget = { token_count: 999999, payment_amount: 0 }
    const again = await source.listTaskTrees()
    expect(again[0].status).toBe('completed')
    expect(again[0].children![0].consumed_budget?.token_count).toBe(137)
  })
})
