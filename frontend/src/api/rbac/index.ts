import type { RbacBindingDataSource, RbacGrantDataSource } from './types'
import { HttpRbacBindingDataSource, HttpRbacGrantDataSource } from './http'

/**
 * 当前数据源：Http（v0.55 契约冻结后切换，Mock 保留在 ./mock 供测试）。
 * 契约（server.py 3244-3247，handler 2086-2240）：
 *   GET  /v1/admin/rbac/bindings                 → { bindings: Binding[] }
 *   POST /v1/admin/rbac/bindings                 ← { principal, tenant_id?, role } → 201 Binding
 *   POST /v1/admin/rbac/bindings/{id}/revoke     → { binding_id, state: "revoked" }
 *   GET  /v1/admin/rbac/grants                   → { grants: Grant[] }
 *   POST /v1/admin/rbac/grants                   ← { source_principal, source_tenant, target_tenant, resources[] } → 201 Grant
 *   POST /v1/admin/rbac/grants/{id}/revoke       → { grant_id, state: "revoked" }
 * 列表语义：吊销即退出列表；租户可见范围由后端按 principal 过滤。
 */
export const rbacBindingSource: RbacBindingDataSource = new HttpRbacBindingDataSource()
export const rbacGrantSource: RbacGrantDataSource = new HttpRbacGrantDataSource()

export type {
  RbacBinding,
  RbacBindingCreateParams,
  RbacBindingDataSource,
  RbacGrant,
  RbacGrantCreateParams,
  RbacGrantDataSource,
  RbacRole,
} from './types'
