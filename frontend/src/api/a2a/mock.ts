import type {
  A2ADeadLetterDataSource,
  A2ADeadLetterItem,
  A2ADeadLetterReplayParams,
  A2ATaskDataSource,
  A2ATaskNode,
} from './types'

/**
 * 演示数据源：复现第十四轮 E2E 验证通过的两跳委托链路
 *（researcher_001 → research-agent → specialist-agent）。
 * 后端 v0.54 推送且任务列表接口就绪后，切换 src/api/a2a/index.ts 到 Http 实现。
 */

const CHILD_TASK: A2ATaskNode = {
  task_id: 'task-demo-child-0452700',
  initiator_agent_id: 'research-agent',
  target_agent_id: 'specialist-agent',
  status: 'completed',
  depth: 1,
  parent_task_id: 'task-demo-root-4219000',
  root_task_id: 'task-demo-root-4219000',
  scope: ['calculate_checksum'],
  deadline: null,
  allow_redelegation: false,
  budget: { token_count: 10000, payment_amount: 0 },
  consumed_budget: { token_count: 137, payment_amount: 0 },
  outcome: {
    status: 'allow',
    checksum: 'sha256:3f2a1c…',
    tool: 'calculate_checksum',
  },
  created_at: '2026-09-16T11:45:58Z',
  updated_at: '2026-09-16T11:46:04Z',
}

const ROOT_TASK: A2ATaskNode = {
  task_id: 'task-demo-root-4219000',
  initiator_agent_id: 'researcher_001',
  target_agent_id: 'research-agent',
  status: 'completed',
  depth: 0,
  parent_task_id: null,
  root_task_id: 'task-demo-root-4219000',
  scope: ['calculate_checksum'],
  deadline: null,
  allow_redelegation: true,
  budget: { token_count: 10000, payment_amount: 0 },
  consumed_budget: { token_count: 137, payment_amount: 0 },
  outcome: {
    child_task_id: 'task-demo-child-0452700',
    child_outcome: { status: 'allow', checksum: 'sha256:3f2a1c…' },
    consumed_budget: { token_count: 137, payment_amount: 0 },
  },
  created_at: '2026-09-16T11:45:58Z',
  updated_at: '2026-09-16T11:46:05Z',
  children: [CHILD_TASK],
}

const TREES: A2ATaskNode[] = [ROOT_TASK]

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T
}

function flatten(nodes: A2ATaskNode[]): A2ATaskNode[] {
  return nodes.flatMap((node) => [node, ...flatten(node.children ?? [])])
}

export class MockA2ATaskDataSource implements A2ATaskDataSource {
  async listTaskTrees(): Promise<A2ATaskNode[]> {
    return clone(TREES)
  }

  async getTask(taskId: string): Promise<A2ATaskNode> {
    const found = flatten(TREES).find((node) => node.task_id === taskId)
    if (!found) throw new Error(`任务不存在：${taskId}`)
    return clone(found)
  }
}

/**
 * 演示数据源：模拟 v0.54 调度体系中的死信队列
 *（预算耗尽重试、远端超时、永久拒绝三种典型失败类别）。
 * 重放语义与 Go 内核一致：revision 校验（乐观并发）、replay_count+1、
 * 状态从 dead_letter 回到 queued（NotBefore 生效前不调度）。
 */

const DEAD_LETTERS: A2ADeadLetterItem[] = [
  {
    assignment_id: 'asg-dead-7781001',
    assignment_kind: 'outbound_delegation',
    tenant_id: 'tenant-a',
    task_id: 'task-demo-root-4219000',
    agent_id: 'research-agent',
    state: 'dead_letter',
    revision: 4,
    route_attempt: 3,
    attempt: 3,
    replay_count: 0,
    failure_class: 'remote_timeout',
    error_code: 'delegation_delivery_timeout',
    not_before: null,
    deadline: '2026-09-16T12:15:58Z',
    outcome: null,
    consumed_budget: { token_count: 300, payment_amount: 0 },
    created_at: '2026-09-16T11:50:12Z',
    updated_at: '2026-09-16T11:55:30Z',
  },
  {
    assignment_id: 'asg-dead-7781002',
    assignment_kind: 'target_execution',
    tenant_id: 'tenant-a',
    task_id: 'task-budget-9910200',
    agent_id: 'specialist-agent',
    state: 'dead_letter',
    revision: 6,
    route_attempt: 2,
    attempt: 5,
    replay_count: 1,
    failure_class: 'pre_dispatch_transient',
    error_code: 'route_attempt_budget_exhausted',
    not_before: '2026-09-17T02:00:00Z',
    deadline: null,
    outcome: null,
    consumed_budget: { token_count: 1200, payment_amount: 0 },
    created_at: '2026-09-16T13:20:41Z',
    updated_at: '2026-09-16T13:28:09Z',
  },
  {
    assignment_id: 'asg-dead-7781003',
    assignment_kind: 'outbound_delegation',
    tenant_id: 'tenant-b',
    task_id: 'task-reject-5503100',
    agent_id: 'audit-agent',
    state: 'dead_letter',
    revision: 2,
    route_attempt: 1,
    attempt: 1,
    replay_count: 0,
    failure_class: 'remote_rejected',
    error_code: 'remote_agent_refused_scope',
    not_before: null,
    deadline: null,
    outcome: null,
    consumed_budget: { token_count: 50, payment_amount: 0 },
    created_at: '2026-09-17T01:12:33Z',
    updated_at: '2026-09-17T01:12:35Z',
  },
]

export class MockA2ADeadLetterDataSource implements A2ADeadLetterDataSource {
  private items: A2ADeadLetterItem[] = clone(DEAD_LETTERS)

  async listDeadLetters(tenantId?: string): Promise<A2ADeadLetterItem[]> {
    // 与 Go 内核 ListDeadLetters 语义一致：仅返回 state=dead_letter 的条目
    const filtered = this.items.filter(
      (item) =>
        item.state === 'dead_letter' && (!tenantId || item.tenant_id === tenantId),
    )
    return clone(filtered)
  }

  async replayDeadLetter(
    assignmentId: string,
    params: A2ADeadLetterReplayParams,
  ): Promise<void> {
    const item = this.items.find((entry) => entry.assignment_id === assignmentId)
    if (!item) throw new Error(`死信不存在：${assignmentId}`)
    if (item.tenant_id !== params.tenant_id) {
      throw new Error(`租户不匹配：${params.tenant_id}`)
    }
    if (params.expected_revision !== item.revision) {
      throw new Error(
        `revision 已变化（期望 ${params.expected_revision}，当前 ${item.revision}），请刷新后重试`,
      )
    }
    // 重放成功：状态回到 queued，退出死信列表；revision 递增防并发重放
    item.state = 'queued'
    item.revision += 1
    item.replay_count += 1
    item.not_before = params.not_before ?? new Date().toISOString()
    item.updated_at = new Date().toISOString()
  }
}
