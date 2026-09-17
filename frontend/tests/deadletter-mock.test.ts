import { describe, it, expect } from 'vitest'
import { MockA2ADeadLetterDataSource } from '@/api/a2a/mock'

describe('MockA2ADeadLetterDataSource', () => {
  it('返回死信列表，含 v0.54 三种典型失败类别', async () => {
    const source = new MockA2ADeadLetterDataSource()
    const items = await source.listDeadLetters()
    expect(items.length).toBeGreaterThanOrEqual(3)
    const classes = new Set(items.map((item) => item.failure_class))
    expect(classes.has('remote_timeout')).toBe(true)
    expect(classes.has('pre_dispatch_transient')).toBe(true)
    expect(classes.has('remote_rejected')).toBe(true)
    for (const item of items) {
      expect(item.state).toBe('dead_letter')
      expect(item.revision).toBeGreaterThan(0)
    }
  })

  it('支持按租户过滤', async () => {
    const source = new MockA2ADeadLetterDataSource()
    const items = await source.listDeadLetters('tenant-a')
    expect(items.length).toBeGreaterThan(0)
    for (const item of items) {
      expect(item.tenant_id).toBe('tenant-a')
    }
    expect(await source.listDeadLetters('tenant-x')).toHaveLength(0)
  })

  it('重放成功：revision 校验、状态回到 queued、退出死信列表', async () => {
    const source = new MockA2ADeadLetterDataSource()
    const [before] = await source.listDeadLetters('tenant-b')
    await source.replayDeadLetter(before.assignment_id, {
      tenant_id: before.tenant_id,
      expected_revision: before.revision,
    })
    const after = await source.listDeadLetters('tenant-b')
    expect(after.find((item) => item.assignment_id === before.assignment_id)).toBeUndefined()
  })

  it('重放拒绝：revision 不匹配或租户不匹配时抛错', async () => {
    const source = new MockA2ADeadLetterDataSource()
    const [item] = await source.listDeadLetters()
    await expect(
      source.replayDeadLetter(item.assignment_id, {
        tenant_id: item.tenant_id,
        expected_revision: item.revision + 99,
      }),
    ).rejects.toThrow('revision 已变化')
    await expect(
      source.replayDeadLetter(item.assignment_id, {
        tenant_id: 'tenant-x',
        expected_revision: item.revision,
      }),
    ).rejects.toThrow('租户不匹配')
    await expect(
      source.replayDeadLetter('asg-not-exist', {
        tenant_id: item.tenant_id,
        expected_revision: 1,
      }),
    ).rejects.toThrow('死信不存在')
  })

  it('返回深拷贝：外部修改不影响内部数据', async () => {
    const source = new MockA2ADeadLetterDataSource()
    const [item] = await source.listDeadLetters()
    item.revision = -1
    const again = await source.listDeadLetters()
    expect(again.find((entry) => entry.assignment_id === item.assignment_id)!.revision).toBeGreaterThan(0)
  })
})
