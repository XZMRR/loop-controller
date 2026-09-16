import {
  getAdminAgents,
  getAdminEntrypoints,
  getAdminIdentity,
  getAdminProfiles,
  type AdminAgent,
} from './python'

export interface AgentConfig {
  agents: AdminAgent[]
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
  identity: Record<string, any>
}

export async function loadAgentsConfig(): Promise<AgentConfig> {
  return { agents: await getAdminAgents() }
}

export async function loadProfilesConfig(): Promise<ProfileConfig> {
  return { profiles: await getAdminProfiles() as ProfileConfig['profiles'] }
}

export async function loadEntrypointsConfig(): Promise<EntrypointsConfig> {
  return getAdminEntrypoints() as Promise<EntrypointsConfig>
}

export async function loadIdentityConfig(): Promise<IdentityConfig> {
  const identity = await getAdminIdentity()
  return { identity: identity.config }
}
