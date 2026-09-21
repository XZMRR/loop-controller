import type { A2ADeadLetterDataSource, A2ATaskDataSource } from './types'
import { HttpA2ADeadLetterDataSource, HttpA2ATaskDataSource } from './http'
import { a2aClient } from '@/api/client'

/** 任务列表统一经 Python Admin 代理，避免浏览器直连 Go 内核。 */
export const a2aTaskSource: A2ATaskDataSource = new HttpA2ATaskDataSource()

/**
 * 死信队列数据源：Http（v0.55 Python 代理已落地，Mock 保留在 ./mock 供测试）。
 * 契约：
 *   GET  /v1/admin/a2a/dead-letters                    → { assignments: TaskAssignment[] }
 *   POST /v1/admin/a2a/dead-letters/{id}/replay        ← { tenant_id, expected_revision, not_before? }
 * 后端按 control tenant 限定范围（与 a2a tasks 代理同模式）；重放乐观 revision，
 * 409 冲突由视图提示后刷新。视图层（DeadLetters.vue）无需任何改动。
 */
export const a2aDeadLetterSource: A2ADeadLetterDataSource =
  new HttpA2ADeadLetterDataSource()

/** Go 内核 readiness（GET /ready，公开端点）；不可达时返回 false 而非抛错 */
export async function fetchKernelReadiness(): Promise<boolean> {
  try {
    const resp = await a2aClient.get('/ready')
    return resp.status >= 200 && resp.status < 300
  } catch {
    return false
  }
}

export type {
  A2ATaskDataSource,
  A2ATaskNode,
  A2ATaskBudget,
  A2ADeadLetterDataSource,
  A2ADeadLetterItem,
  A2ADeadLetterReplayParams,
} from './types'
