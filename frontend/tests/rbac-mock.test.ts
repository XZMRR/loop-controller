import { describe, expect, it } from 'vitest'
import {
  MockRbacBindingDataSource,
  MockRbacGrantDataSource,
} from '@/api/rbac/mock'

describe('MockRbacBindingDataSource', () => {
  it('列表返回未吊销绑定，字段与后端 payload 对齐', async () => {
    const source = new MockRbacBindingDataSource()
    const bindings = await source.listBindings()
    expect(bindings.length).toBeGreaterThanOrEqual(5)
    for (const binding of bindings) {
      expect(binding.binding_id).toMatch(/^rbind-/)
      expect(binding.principal).toBeTruthy()
      expect(typeof binding.role).toBe('string')
      expect(binding.granted_by).toBeTruthy()
      expect(binding.created_at).toBeTruthy()
    }
    // 数据里既有平台级绑定（tenant_id=null）也有租户级绑定
    expect(bindings.some((b) => b.tenant_id === null)).toBe(true)
    expect(bindings.some((b) => b.tenant_id !== null)).toBe(true)
  })

  it('创建租户角色绑定成功并进入列表', async () => {
    const source = new MockRbacBindingDataSource()
    const before = (await source.listBindings()).length
    const created = await source.createBinding({
      principal: 'new-creator',
      tenant_id: 'tenant-a',
      role: 'policy_creator',
    })
    expect(created.binding_id).toMatch(/^rbind-/)
    expect(created.tenant_id).toBe('tenant-a')
    expect(created.granted_by).toBe('console-admin')
    expect((await source.listBindings()).length).toBe(before + 1)
  })

  it('platform_admin 挂租户被拒绝（对齐 server.py:2004）', async () => {
    const source = new MockRbacBindingDataSource()
    await expect(
      source.createBinding({
        principal: 'intruder',
        tenant_id: 'tenant-a',
        role: 'platform_admin',
      }),
    ).rejects.toThrow('platform_admin 仅限平台级绑定')
  })

  it('租户角色缺 tenant_id 被拒绝（对齐 server.py:2007 语义）', async () => {
    const source = new MockRbacBindingDataSource()
    await expect(
      source.createBinding({ principal: 'no-tenant', role: 'approver' }),
    ).rejects.toThrow('invalid_binding')
  })

  it('吊销后退出列表，响应形状为 { binding_id, state: revoked }', async () => {
    const source = new MockRbacBindingDataSource()
    const target = (await source.listBindings())[0]
    const result = await source.revokeBinding(target.binding_id)
    expect(result).toEqual({ binding_id: target.binding_id, state: 'revoked' })
    const after = await source.listBindings()
    expect(after.find((b) => b.binding_id === target.binding_id)).toBeUndefined()
  })

  it('吊销不存在的绑定返回 not_found', async () => {
    const source = new MockRbacBindingDataSource()
    await expect(source.revokeBinding('rbind-nope')).rejects.toThrow('not_found')
  })

  it('返回对象是深拷贝，外部修改不污染内部数据', async () => {
    const source = new MockRbacBindingDataSource()
    const list = await source.listBindings()
    list[0].principal = 'tampered'
    const again = await source.listBindings()
    expect(again[0].principal).not.toBe('tampered')
  })
})

describe('MockRbacGrantDataSource', () => {
  it('列表返回未吊销授权，resources 为字符串数组', async () => {
    const source = new MockRbacGrantDataSource()
    const grants = await source.listGrants()
    expect(grants.length).toBeGreaterThanOrEqual(2)
    for (const grant of grants) {
      expect(grant.grant_id).toMatch(/^rgrant-/)
      expect(Array.isArray(grant.resources)).toBe(true)
      expect(grant.resources.length).toBeGreaterThan(0)
      expect(grant.source_tenant).not.toBe(grant.target_tenant)
    }
  })

  it('创建授权成功并进入列表', async () => {
    const source = new MockRbacGrantDataSource()
    const before = (await source.listGrants()).length
    const created = await source.createGrant({
      source_principal: 'cross-svc',
      source_tenant: 'tenant-a',
      target_tenant: 'tenant-b',
      resources: ['policy.publish.global'],
    })
    expect(created.grant_id).toMatch(/^rgrant-/)
    expect(created.resources).toEqual(['policy.publish.global'])
    expect((await source.listGrants()).length).toBe(before + 1)
  })

  it('源租户等于目标租户被拒绝', async () => {
    const source = new MockRbacGrantDataSource()
    await expect(
      source.createGrant({
        source_principal: 'self-svc',
        source_tenant: 'tenant-a',
        target_tenant: 'tenant-a',
        resources: ['policy.publish.global'],
      }),
    ).rejects.toThrow('invalid_grant')
  })

  it('空 resources 被拒绝', async () => {
    const source = new MockRbacGrantDataSource()
    await expect(
      source.createGrant({
        source_principal: 'empty-svc',
        source_tenant: 'tenant-a',
        target_tenant: 'tenant-b',
        resources: [],
      }),
    ).rejects.toThrow('invalid_grant')
  })

  it('吊销后退出列表，响应形状为 { grant_id, state: revoked }', async () => {
    const source = new MockRbacGrantDataSource()
    const target = (await source.listGrants())[0]
    const result = await source.revokeGrant(target.grant_id)
    expect(result).toEqual({ grant_id: target.grant_id, state: 'revoked' })
    const after = await source.listGrants()
    expect(after.find((g) => g.grant_id === target.grant_id)).toBeUndefined()
  })

  it('吊销不存在的授权返回 not_found', async () => {
    const source = new MockRbacGrantDataSource()
    await expect(source.revokeGrant('rgrant-nope')).rejects.toThrow('not_found')
  })
})
