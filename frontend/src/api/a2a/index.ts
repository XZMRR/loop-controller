import type { A2ATaskDataSource } from './types'
import { MockA2ATaskDataSource } from './mock'

/**
 * 当前数据源：Mock（演示）。
 * 后端 v0.54 推送、任务列表接口就绪后，替换为 Http 实现：
 *   class HttpA2ATaskDataSource implements A2ATaskDataSource {
 *     // 经 pythonClient /a2a/v1/tasks 拉取并按 parent_task_id 组树
 *   }
 * 视图层（TaskTree.vue / A2A.vue）无需任何改动。
 */
export const a2aTaskSource: A2ATaskDataSource = new MockA2ATaskDataSource()

export type { A2ATaskDataSource, A2ATaskNode, A2ATaskBudget } from './types'
