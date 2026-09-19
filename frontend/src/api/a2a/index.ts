import type { A2ADeadLetterDataSource, A2ATaskDataSource } from './types'
import { MockA2ADeadLetterDataSource } from './mock'
import { HttpA2ATaskDataSource } from './http'
import { a2aClient } from '@/api/client'

/** 任务列表统一经 Python Admin 代理，避免浏览器直连 Go 内核。 */
export const a2aTaskSource: A2ATaskDataSource = new HttpA2ATaskDataSource()

/**
 * 死信队列数据源：Mock（演示）。
 * 契约已由 Go 内核定义（v0.54）：
 *   GET  /a2a/v1/dead-letters?tenant_id=   → { assignments: TaskAssignment[] }
 *   POST /a2a/v1/dead-letters/{id}/replay  ← { tenant_id, expected_revision, not_before? }
 * Http 实现直接走 a2aClient；视图层（DeadLetters.vue）无需任何改动。
 */
export const a2aDeadLetterSource: A2ADeadLetterDataSource =
  new MockA2ADeadLetterDataSource()

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
