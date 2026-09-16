import type { A2ATaskDataSource, A2ATaskNode } from './types'

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
