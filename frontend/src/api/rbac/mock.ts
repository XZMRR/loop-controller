import type {
  RbacBinding,
  RbacBindingCreateParams,
  RbacBindingDataSource,
  RbacGrant,
  RbacGrantCreateParams,
  RbacGrantDataSource,
  RbacRole,
} from './types'

/**
 * 演示数据源：模拟 v0.52 统一访问控制平面的角色绑定与跨租户授权。
 * 语义与 Python runtime（server.py:1974-2128 + rbac/store.py）一致：
 * - 列表只返回未吊销记录（吊销 = 软删，退出列表）；
 * - 创建校验与后端相同：platform_admin 角色必须 tenant_id=null，
 *   其余角色必须挂租户；非法 role 返回 invalid_binding 语义的错误；
 * - revoke 返回 { id, state: 'revoked' }，与后端响应形状一致。
 */

const VALID_ROLES: RbacRole[] = [
  'platform_admin',
  'tenant_admin',
  'policy_creator',
  'policy_validator',
  'policy_publisher',
  'policy_auditor',
  'approver',
]

const BINDINGS: RbacBinding[] = [
  {
    binding_id: 'rbind-7001',
    principal: 'zhang.wei',
    tenant_id: null,
    role: 'platform_admin',
    granted_by: 'system-bootstrap',
    created_at: '2026-09-10T02:00:00Z',
  },
  {
    binding_id: 'rbind-7002',
    principal: 'li.na',
    tenant_id: 'tenant-a',
    role: 'tenant_admin',
    granted_by: 'zhang.wei',
    created_at: '2026-09-10T02:05:12Z',
  },
  {
    binding_id: 'rbind-7003',
    principal: 'dev-svc-research',
    tenant_id: 'tenant-a',
    role: 'policy_creator',
    granted_by: 'li.na',
    created_at: '2026-09-11T08:30:45Z',
  },
  {
    binding_id: 'rbind-7004',
    principal: 'audit-bot',
    tenant_id: 'tenant-a',
    role: 'policy_auditor',
    granted_by: 'li.na',
    created_at: '2026-09-11T09:12:03Z',
  },
  {
    binding_id: 'rbind-7005',
    principal: 'wang.fang',
    tenant_id: 'tenant-b',
    role: 'tenant_admin',
    granted_by: 'zhang.wei',
    created_at: '2026-09-12T03:22:18Z',
  },
  {
    binding_id: 'rbind-7006',
    principal: 'release-svc',
    tenant_id: 'tenant-b',
    role: 'policy_publisher',
    granted_by: 'wang.fang',
    created_at: '2026-09-12T03:40:56Z',
  },
  {
    binding_id: 'rbind-7007',
    principal: 'qa-bot',
    tenant_id: 'tenant-b',
    role: 'approver',
    granted_by: 'wang.fang',
    created_at: '2026-09-13T06:15:29Z',
  },
]

const GRANTS: RbacGrant[] = [
  {
    grant_id: 'rgrant-5001',
    source_principal: 'dev-svc-research',
    source_tenant: 'tenant-a',
    target_tenant: 'tenant-shared',
    resources: ['policy.publish.global', 'policy.validate.global'],
    granted_by: 'zhang.wei',
    created_at: '2026-09-11T10:02:41Z',
  },
  {
    grant_id: 'rgrant-5002',
    source_principal: 'release-svc',
    source_tenant: 'tenant-b',
    target_tenant: 'tenant-shared',
    resources: ['policy.publish.global'],
    granted_by: 'zhang.wei',
    created_at: '2026-09-12T04:11:07Z',
  },
  {
    grant_id: 'rgrant-5003',
    source_principal: 'audit-bot',
    source_tenant: 'tenant-a',
    target_tenant: 'tenant-b',
    resources: ['audit.read.cross_tenant'],
    granted_by: 'zhang.wei',
    created_at: '2026-09-13T02:30:15Z',
  },
]

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T
}

let seq = 7008
let grantSeq = 5004

export class MockRbacBindingDataSource implements RbacBindingDataSource {
  private items: RbacBinding[] = clone(BINDINGS)

  async listBindings(): Promise<RbacBinding[]> {
    return clone(this.items)
  }

  async createBinding(params: RbacBindingCreateParams): Promise<RbacBinding> {
    if (!params.principal) {
      throw new Error('invalid_binding：principal 不能为空')
    }
    if (!VALID_ROLES.includes(params.role)) {
      throw new Error(`invalid_binding：未知角色 ${params.role}`)
    }
    // 与 server.py:2004 一致：platform_admin 必须 tenant_id=null
    if (params.role === 'platform_admin' && params.tenant_id != null) {
      throw new Error('forbidden：platform_admin 仅限平台级绑定（tenant_id 必须为平台级）')
    }
    // 与 server.py:2007 一致：非平台角色必须挂租户
    if (params.role !== 'platform_admin' && !params.tenant_id) {
      throw new Error('invalid_binding：租户角色必须指定 tenant_id')
    }
    const binding: RbacBinding = {
      binding_id: `rbind-${seq++}`,
      principal: params.principal,
      tenant_id: params.tenant_id ?? null,
      role: params.role,
      granted_by: 'console-admin',
      created_at: new Date().toISOString(),
    }
    this.items.push(binding)
    return clone(binding)
  }

  async revokeBinding(bindingId: string): Promise<{ binding_id: string; state: 'revoked' }> {
    const idx = this.items.findIndex((entry) => entry.binding_id === bindingId)
    if (idx < 0) throw new Error(`not_found：绑定不存在 ${bindingId}`)
    // 与 store.revoke_binding 软删语义一致：退出列表
    this.items.splice(idx, 1)
    return { binding_id: bindingId, state: 'revoked' }
  }
}

export class MockRbacGrantDataSource implements RbacGrantDataSource {
  private items: RbacGrant[] = clone(GRANTS)

  async listGrants(): Promise<RbacGrant[]> {
    return clone(this.items)
  }

  async createGrant(params: RbacGrantCreateParams): Promise<RbacGrant> {
    if (!params.source_principal || !params.source_tenant || !params.target_tenant) {
      throw new Error('invalid_grant：source_principal / source_tenant / target_tenant 均必填')
    }
    if (!params.resources?.length) {
      throw new Error('invalid_grant：resources 至少一项')
    }
    if (params.source_tenant === params.target_tenant) {
      throw new Error('invalid_grant：source_tenant 与 target_tenant 不能相同')
    }
    const grant: RbacGrant = {
      grant_id: `rgrant-${grantSeq++}`,
      source_principal: params.source_principal,
      source_tenant: params.source_tenant,
      target_tenant: params.target_tenant,
      resources: [...params.resources],
      granted_by: 'console-admin',
      created_at: new Date().toISOString(),
    }
    this.items.push(grant)
    return clone(grant)
  }

  async revokeGrant(grantId: string): Promise<{ grant_id: string; state: 'revoked' }> {
    const idx = this.items.findIndex((entry) => entry.grant_id === grantId)
    if (idx < 0) throw new Error(`not_found：授权不存在 ${grantId}`)
    // 与 store.revoke_grant 软删语义一致：退出列表
    this.items.splice(idx, 1)
    return { grant_id: grantId, state: 'revoked' }
  }
}
