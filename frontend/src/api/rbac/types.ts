// RBAC 绑定管理契约层。
// 字段形状以 Python runtime server.py 的 _binding_payload / _grant_payload 为准
//（server.py:1951-1972；路由 server.py:3020-3023，handler server.py:1974-2128）。
// 数据 100% 在 Python 侧（SQLite rbac_role_bindings / rbac_cross_tenant_grants），
// 与 Go A2A 内核无关；Http 实现应走 pythonClient（/api/python），不要复用 a2aClient。

/** 后端 Role 枚举（rbac/models.py），非法值在创建时返回 400 invalid_binding */
export type RbacRole =
  | 'platform_admin'
  | 'tenant_admin'
  | 'policy_creator'
  | 'policy_validator'
  | 'policy_publisher'
  | 'policy_auditor'
  | 'approver'

export interface RbacBinding {
  binding_id: string
  principal: string
  /** null = 平台级绑定 */
  tenant_id: string | null
  role: RbacRole
  granted_by: string
  created_at: string
}

export interface RbacGrant {
  grant_id: string
  source_principal: string
  source_tenant: string
  target_tenant: string
  resources: string[]
  granted_by: string
  created_at: string
}

/** 对应 POST /v1/admin/rbac/bindings 请求体 */
export interface RbacBindingCreateParams {
  principal: string
  tenant_id?: string | null
  role: RbacRole
}

/** 对应 POST /v1/admin/rbac/grants 请求体 */
export interface RbacGrantCreateParams {
  source_principal: string
  source_tenant: string
  target_tenant: string
  resources: string[]
}

export interface RbacBindingDataSource {
  /** 仅返回未吊销绑定（后端列表语义，吊销即退出列表） */
  listBindings(): Promise<RbacBinding[]>
  createBinding(params: RbacBindingCreateParams): Promise<RbacBinding>
  revokeBinding(bindingId: string): Promise<{ binding_id: string; state: 'revoked' }>
}

export interface RbacGrantDataSource {
  /** 仅返回未吊销授权（后端列表语义，吊销即退出列表） */
  listGrants(): Promise<RbacGrant[]>
  createGrant(params: RbacGrantCreateParams): Promise<RbacGrant>
  revokeGrant(grantId: string): Promise<{ grant_id: string; state: 'revoked' }>
}
