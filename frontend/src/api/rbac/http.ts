// RBAC 绑定管理 Http 数据源（v0.55 契约冻结后切换）。
// 路由：GET/POST /v1/admin/rbac/bindings、POST /v1/admin/rbac/bindings/{id}/revoke、
//       GET/POST /v1/admin/rbac/grants、POST /v1/admin/rbac/grants/{id}/revoke
// 负载：server.py _binding_payload/_grant_payload（与 types.ts 一致）。
// 列表语义：吊销即退出列表（store.list_all_bindings 不含 revoked）；
// 租户过滤由后端按 principal 完成，前端视图再做本地筛选。

import { pythonClient } from '@/api/client'
import type {
  RbacBinding,
  RbacBindingCreateParams,
  RbacBindingDataSource,
  RbacGrant,
  RbacGrantCreateParams,
  RbacGrantDataSource,
} from './types'

export class HttpRbacBindingDataSource implements RbacBindingDataSource {
  async listBindings(): Promise<RbacBinding[]> {
    const { data } = await pythonClient.get<{ bindings?: RbacBinding[] }>(
      '/v1/admin/rbac/bindings',
    )
    return data.bindings ?? []
  }

  async createBinding(params: RbacBindingCreateParams): Promise<RbacBinding> {
    const { data } = await pythonClient.post<RbacBinding>('/v1/admin/rbac/bindings', params)
    return data
  }

  async revokeBinding(bindingId: string): Promise<{ binding_id: string; state: 'revoked' }> {
    const { data } = await pythonClient.post<{ binding_id: string; state: 'revoked' }>(
      `/v1/admin/rbac/bindings/${encodeURIComponent(bindingId)}/revoke`,
    )
    return data
  }
}

export class HttpRbacGrantDataSource implements RbacGrantDataSource {
  async listGrants(): Promise<RbacGrant[]> {
    const { data } = await pythonClient.get<{ grants?: RbacGrant[] }>('/v1/admin/rbac/grants')
    return data.grants ?? []
  }

  async createGrant(params: RbacGrantCreateParams): Promise<RbacGrant> {
    const { data } = await pythonClient.post<RbacGrant>('/v1/admin/rbac/grants', params)
    return data
  }

  async revokeGrant(grantId: string): Promise<{ grant_id: string; state: 'revoked' }> {
    const { data } = await pythonClient.post<{ grant_id: string; state: 'revoked' }>(
      `/v1/admin/rbac/grants/${encodeURIComponent(grantId)}/revoke`,
    )
    return data
  }
}
