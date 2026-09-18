import type { RbacBindingDataSource, RbacGrantDataSource } from './types'
import { MockRbacBindingDataSource, MockRbacGrantDataSource } from './mock'

/**
 * 当前数据源：Mock（演示）。
 * 契约已由 Python runtime 定义（v0.52）：
 *   GET  /v1/admin/rbac/bindings                 → { bindings: Binding[] }
 *   POST /v1/admin/rbac/bindings                 ← { principal, tenant_id?, role } → 201 Binding
 *   POST /v1/admin/rbac/bindings/{id}/revoke     → { binding_id, state: "revoked" }
 *   GET  /v1/admin/rbac/grants                   → { grants: Grant[] }
 *   POST /v1/admin/rbac/grants                   ← { source_principal, source_tenant, target_tenant, resources[] } → 201 Grant
 *   POST /v1/admin/rbac/grants/{id}/revoke       → { grant_id, state: "revoked" }
 * Http 实现直接走 pythonClient（/api/python）；视图层（RbacBindings.vue）无需任何改动。
 */
export const rbacBindingSource: RbacBindingDataSource = new MockRbacBindingDataSource()
export const rbacGrantSource: RbacGrantDataSource = new MockRbacGrantDataSource()

export type {
  RbacBinding,
  RbacBindingCreateParams,
  RbacBindingDataSource,
  RbacGrant,
  RbacGrantCreateParams,
  RbacGrantDataSource,
  RbacRole,
} from './types'
