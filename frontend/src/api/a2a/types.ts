// A2A 任务树契约层。
// 字段形状以 Go 内核 /a2a/v1/tasks 实际响应为准（第十四轮 E2E 验证通过）。
// v0.54 整合后若响应形状变化，仅需同步本文件与 http 数据源适配器。

export interface A2ATaskBudget {
  token_count: number
  payment_amount: number
  currency?: string
}

export interface A2ATaskNode {
  task_id: string
  initiator_agent_id: string
  target_agent_id: string
  status: string
  depth: number
  parent_task_id: string | null
  root_task_id: string
  scope: string[]
  deadline: string | null
  allow_redelegation: boolean
  budget: A2ATaskBudget
  reserved_budget?: A2ATaskBudget
  consumed_budget?: A2ATaskBudget
  outcome?: Record<string, any> | null
  error_code?: string
  created_at: string
  updated_at: string
  children?: A2ATaskNode[]
}

export interface A2ATaskDataSource {
  /** 返回若干棵委托任务树（根任务 + 子孙） */
  listTaskTrees(): Promise<A2ATaskNode[]>
  getTask(taskId: string): Promise<A2ATaskNode>
}

// Dead-letter（死信）契约层。
// 字段形状以 Go 内核 models.TaskAssignment 序列化为准
//（go/internal/models/models.go，GET /a2a/v1/dead-letters 返回 {assignments: [...]}）。

export interface A2ADeadLetterItem {
  assignment_id: string
  assignment_kind: string // target_execution | outbound_delegation
  tenant_id: string
  task_id: string
  agent_id: string
  state: string // 恒为 dead_letter
  revision: number
  route_attempt: number
  attempt: number
  replay_count: number
  failure_class: string
  error_code?: string
  not_before: string | null
  deadline: string | null
  outcome?: Record<string, any> | null
  execution_receipt?: Record<string, any> | null
  consumed_budget?: { token_count?: number; payment_amount?: number }
  created_at: string
  updated_at: string
}

/** 对应 POST /a2a/v1/dead-letters/{id}/replay 请求体 */
export interface A2ADeadLetterReplayParams {
  tenant_id: string
  expected_revision: number
  not_before?: string
}

export interface A2ADeadLetterDataSource {
  listDeadLetters(tenantId?: string): Promise<A2ADeadLetterItem[]>
  replayDeadLetter(assignmentId: string, params: A2ADeadLetterReplayParams): Promise<void>
}
