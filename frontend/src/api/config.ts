import axios from 'axios'
import {
  getAdminAgents,
  getAdminEntrypoints,
  getAdminIdentity,
  getAdminProfiles,
} from './python'

export async function loadYaml<T>(path: string): Promise<T> {
  const { data } = await axios.get<string>(path)
  const yaml = await import('js-yaml')
  return yaml.load(data) as T
}

async function tryApi<T>(fn: () => Promise<T>): Promise<T | null> {
  try {
    return await fn()
  } catch {
    return null
  }
}

export interface AgentConfig {
  agents?: Array<{
    agent_id: string
    name?: string
    profile_id?: string
    owner_id?: string
    identity?: Record<string, any>
  }>
  users?: Array<{
    user_id: string
    display_name?: string
  }>
}

export interface ToolPolicy {
  allowed?: boolean
  require_approval?: boolean
  allowed_args?: Record<string, any>
  denied_args?: Record<string, any>
  max_calls_per_task?: number
}

export interface ProfileConfig {
  profiles?: Array<{
    profile_id: string
    description?: string
    max_budget_token?: number
    max_budget_payment?: number
    tools: Record<string, ToolPolicy>
  }>
}

export interface EntrypointsConfig {
  entrypoints: Record<string, {
    auth: string
    require_auth: boolean
    admin_agent_ids?: string[]
  }>
}

export interface IdentityConfig {
  identity: {
    provider: string
    static?: {
      allowed_tokens: Array<{
        token: string
        agent_id: string
        user_id: string
      }>
    }
    jwt?: Record<string, any>
    mtls?: Record<string, any>
  }
}

export async function loadAgentsConfig(): Promise<AgentConfig> {
  // 优先走后端 Admin API（含吊销状态）；users 列表后端暂无接口，仍从 YAML 补充
  const apiAgents = await tryApi(getAdminAgents)
  if (apiAgents) {
    const yaml = await tryApi(() => loadYaml<AgentConfig>('/config/agents.yaml'))
    return { agents: apiAgents, users: yaml?.users }
  }
  return loadYaml<AgentConfig>('/config/agents.yaml')
}

export async function loadProfilesConfig(): Promise<ProfileConfig> {
  const apiProfiles = await tryApi(getAdminProfiles)
  if (apiProfiles) {
    return { profiles: apiProfiles as ProfileConfig['profiles'] }
  }
  return loadYaml<ProfileConfig>('/config/profiles.yaml')
}

export async function loadEntrypointsConfig(): Promise<EntrypointsConfig> {
  const apiEntrypoints = await tryApi(getAdminEntrypoints)
  if (apiEntrypoints) {
    return apiEntrypoints as EntrypointsConfig
  }
  return loadYaml<EntrypointsConfig>('/config/entrypoints.yaml')
}

export async function loadIdentityConfig(): Promise<IdentityConfig> {
  const apiIdentity = await tryApi(getAdminIdentity)
  if (apiIdentity) {
    // config 为已脱敏的 identity 段（含 provider 键）
    return { identity: apiIdentity.config } as IdentityConfig
  }
  return loadYaml<IdentityConfig>('/config/identity.yaml')
}
