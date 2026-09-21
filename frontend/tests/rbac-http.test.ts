import { beforeEach, describe, expect, it, vi } from 'vitest'

const { get, post } = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }))
vi.mock('@/api/client', () => ({ pythonClient: { get, post } }))

import {
  HttpRbacBindingDataSource,
  HttpRbacGrantDataSource,
} from '@/api/rbac/http'

const BINDING = {
  binding_id: 'b/1',
  principal: 'agent-a',
  tenant_id: 'tenant-a',
  role: 'approver',
  granted_by: 'admin',
  created_at: '2026-09-19T10:00:00Z',
}

const GRANT = {
  grant_id: 'g/1',
  source_principal: 'agent-a',
  source_tenant: 'tenant-a',
  target_tenant: 'tenant-b',
  resources: ['s3://bucket'],
  granted_by: 'admin',
  created_at: '2026-09-19T10:00:00Z',
}

describe('HttpRbacBindingDataSource', () => {
  beforeEach(() => {
    get.mockReset()
    post.mockReset()
  })

  it('列表解包 bindings，缺省空数组', async () => {
    get.mockResolvedValue({ data: { bindings: [BINDING] } })
    expect(await new HttpRbacBindingDataSource().listBindings()).toEqual([BINDING])
    expect(get).toHaveBeenCalledWith('/v1/admin/rbac/bindings')

    get.mockResolvedValue({ data: {} })
    expect(await new HttpRbacBindingDataSource().listBindings()).toEqual([])
  })

  it('创建绑定 POST 请求体与返回负载', async () => {
    post.mockResolvedValue({ data: BINDING })
    const params = { principal: 'agent-a', tenant_id: null, role: 'approver' as const }
    expect(await new HttpRbacBindingDataSource().createBinding(params)).toEqual(BINDING)
    expect(post).toHaveBeenCalledWith('/v1/admin/rbac/bindings', params)
  })

  it('吊销走 /revoke 子路由且 ID 编码', async () => {
    post.mockResolvedValue({ data: { binding_id: 'b/1', state: 'revoked' } })
    await new HttpRbacBindingDataSource().revokeBinding('b/1')
    expect(post).toHaveBeenCalledWith('/v1/admin/rbac/bindings/b%2F1/revoke')
  })
})

describe('HttpRbacGrantDataSource', () => {
  beforeEach(() => {
    get.mockReset()
    post.mockReset()
  })

  it('列表解包 grants，缺省空数组', async () => {
    get.mockResolvedValue({ data: { grants: [GRANT] } })
    expect(await new HttpRbacGrantDataSource().listGrants()).toEqual([GRANT])
    expect(get).toHaveBeenCalledWith('/v1/admin/rbac/grants')
  })

  it('创建授权 POST 请求体与返回负载', async () => {
    post.mockResolvedValue({ data: GRANT })
    const params = {
      source_principal: 'agent-a',
      source_tenant: 'tenant-a',
      target_tenant: 'tenant-b',
      resources: ['s3://bucket'],
    }
    expect(await new HttpRbacGrantDataSource().createGrant(params)).toEqual(GRANT)
    expect(post).toHaveBeenCalledWith('/v1/admin/rbac/grants', params)
  })

  it('吊销走 /revoke 子路由且 ID 编码', async () => {
    post.mockResolvedValue({ data: { grant_id: 'g/1', state: 'revoked' } })
    await new HttpRbacGrantDataSource().revokeGrant('g/1')
    expect(post).toHaveBeenCalledWith('/v1/admin/rbac/grants/g%2F1/revoke')
  })
})
