import { pythonClient } from '@/api/client'
import type { A2ATaskDataSource, A2ATaskNode } from './types'

export interface KernelTask {
  task_id: string
  initiator_agent_id: string
  target_agent_id: string
  status: string
  delegation_depth?: number
  parent_task_id?: string | null
  root_task_id?: string
  allowed_tools?: string[]
  deadline?: string | null
  allow_redelegation: boolean
  budget: A2ATaskNode['budget']
  reserved_budget?: A2ATaskNode['reserved_budget']
  consumed_budget?: A2ATaskNode['consumed_budget']
  outcome?: A2ATaskNode['outcome']
  error_code?: string
  created_at: string
  updated_at: string
}

export function normalizeTask(task: KernelTask): A2ATaskNode {
  return {
    task_id: task.task_id,
    initiator_agent_id: task.initiator_agent_id,
    target_agent_id: task.target_agent_id,
    status: task.status,
    depth: task.delegation_depth ?? 0,
    parent_task_id: task.parent_task_id || null,
    root_task_id: task.root_task_id || task.task_id,
    scope: task.allowed_tools ?? [],
    deadline: task.deadline ?? null,
    allow_redelegation: task.allow_redelegation,
    budget: task.budget,
    reserved_budget: task.reserved_budget,
    consumed_budget: task.consumed_budget,
    outcome: task.outcome,
    error_code: task.error_code,
    created_at: task.created_at,
    updated_at: task.updated_at,
  }
}

export function buildTaskTrees(tasks: KernelTask[]): A2ATaskNode[] {
  const nodes = new Map<string, A2ATaskNode>()
  const order = new Map<string, number>()
  tasks.forEach((task, index) => {
    if (!nodes.has(task.task_id)) {
      nodes.set(task.task_id, normalizeTask(task))
      order.set(task.task_id, index)
    }
  })

  const unsafeParents = new Set<string>()
  const state = new Map<string, 0 | 1 | 2>()
  const visit = (id: string) => {
    if (state.get(id) === 2) return
    const path: string[] = []
    const positions = new Map<string, number>()
    let current: string | null = id
    while (current && nodes.has(current) && state.get(current) !== 2) {
      const cycleStart = positions.get(current)
      if (cycleStart !== undefined) {
        for (let i = cycleStart; i < path.length; i += 1) unsafeParents.add(path[i])
        break
      }
      positions.set(current, path.length)
      path.push(current)
      state.set(current, 1)
      current = nodes.get(current)?.parent_task_id ?? null
    }
    path.forEach((taskId) => state.set(taskId, 2))
  }
  nodes.forEach((_, id) => visit(id))

  const roots: A2ATaskNode[] = []
  nodes.forEach((node) => {
    const parentId = node.parent_task_id
    const parent = parentId && !unsafeParents.has(node.task_id) ? nodes.get(parentId) : undefined
    if (!parent || parent === node) {
      node.parent_task_id = null
      roots.push(node)
      return
    }
    ;(parent.children ??= []).push(node)
  })

  const compare = (left: A2ATaskNode, right: A2ATaskNode) => {
    const byCreatedAt = left.created_at.localeCompare(right.created_at)
    return byCreatedAt || (order.get(left.task_id) ?? 0) - (order.get(right.task_id) ?? 0)
  }
  const sortTree = (nodesToSort: A2ATaskNode[]) => {
    nodesToSort.sort(compare)
    nodesToSort.forEach((node) => {
      if (node.children) sortTree(node.children)
    })
  }
  sortTree(roots)
  return roots
}

export class HttpA2ATaskDataSource implements A2ATaskDataSource {
  async listTaskTrees(): Promise<A2ATaskNode[]> {
    const { data } = await pythonClient.get<{ tasks?: KernelTask[] }>('/v1/admin/a2a/tasks')
    return buildTaskTrees(data.tasks ?? [])
  }

  async getTask(taskId: string): Promise<A2ATaskNode> {
    const { data } = await pythonClient.get<KernelTask>(
      `/v1/admin/a2a/tasks/${encodeURIComponent(taskId)}`,
    )
    return normalizeTask(data)
  }
}
