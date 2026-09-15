import axios from 'axios'

export async function loadYaml<T>(path: string): Promise<T> {
  const { data } = await axios.get<string>(path)
  const yaml = await import('js-yaml')
  return yaml.load(data) as T
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
  return loadYaml<AgentConfig>('/config/agents.yaml')
}

export async function loadProfilesConfig(): Promise<ProfileConfig> {
  return loadYaml<ProfileConfig>('/config/profiles.yaml')
}

export async function loadEntrypointsConfig(): Promise<EntrypointsConfig> {
  return loadYaml<EntrypointsConfig>('/config/entrypoints.yaml')
}

export async function loadIdentityConfig(): Promise<IdentityConfig> {
  return loadYaml<IdentityConfig>('/config/identity.yaml')
}
