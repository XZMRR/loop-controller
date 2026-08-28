"""配置加载器（§4.1）.

进程启动时一次性加载全部静态配置，构造不可变 ``AppConfig``。
运行期不监听、不热更新（改配置 = 重启进程）。

启动校验（任一失败则拒绝启动，fail-closed）：
1. 每个 Agent 引用的 profile_id 必须存在；
2. 每个 Profile 的 tools 中工具名必须能在 tool_mapping 中找到；
3. ``policy_dir`` 下必须存在 ``default.rego`` 且 OPA 试查询返回结构合法的 deny；
4. 审计/决策日志目录可写；
5. 全部 glob 模式可编译；
6. 全部掩码正则可编译；
7. 审批人必须存在于 users 中；
8. 若 ``audit_hash_algo=hmac-sha256``，则 ``audit_hmac_key_env`` 指向的环境变量必须存在且能解析为 ≥32 字节的随机 key。
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlparse

import httpx
import yaml
from pydantic import ValidationError

from loop_controller.executors.harness_models import (
    DockerBackendConfig,
    HarnessToolSpec,
    HTTPBackendConfig,
    SubprocessBackendConfig,
)
from loop_controller.executors.http_models import HTTPToolSpec, resolve_env_refs
from loop_controller.executors.local_function_models import LocalFunctionSpec
from loop_controller.models import (
    Agent,
    AuditRule,
    AuditRuleConditions,
    AuditRules,
    AuthorityConditions,
    AuthorityGrantRule,
    AuthorityRules,
    BudgetCost,
    CapabilityProfile,
    ToolPermission,
)
from loop_controller.utils.globmatch import compile_glob

POLICY_PACKAGE = "loop_controller.tool_permission"


class ConfigValidationError(Exception):
    """配置加载或校验失败（启动拒绝）。"""


# ---------------------------------------------------------------------------
# 配置类型（infra 层，非 §3 核心 Schema）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MCPServerConfig:
    name: str
    command: list[str]
    transport: str = "stdio"


@dataclass(frozen=True)
class LLMPlannerConfig:
    """LLMPlanner 配置（T3.5）。

    ``api_key_env`` 仅存储环境变量名，密钥在运行期从该变量读取，不落盘、不写入审计日志。
    """

    enabled: bool = False
    provider: str = "openai-compatible"
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    api_key_env: str = "LLM_API_KEY"
    max_tokens: int = 1000
    temperature: float = 0.2
    timeout_s: int = 30


@dataclass(frozen=True)
class ToolMappingEntry:
    server: str
    mcp_name: str
    cost_per_call: int = 1  # v1.1（评审#3）：每次调用的 token 成本估算，供 BudgetLedger 计费


@dataclass(frozen=True)
class PermissionCondition:
    """组合规则中的一个条件（§6.2）。

    每行只包含四类字段之一；``when_all`` 由多个条件组成，全部满足才命中。
    """

    history_tool: str | None = None
    history_arg_match: dict[str, str] | None = None
    current_tool: str | None = None
    current_arg_not_match: dict[str, str] | None = None


@dataclass(frozen=True)
class PermissionRule:
    id: str
    description: str
    when_all: list[PermissionCondition]
    action: Literal["deny", "require_approval"]
    reason: str
    risk_tags: list[str] = field(default_factory=list)  # v0.10.0：能力组合风险标签
    score: int = 0  # v0.10.0：组合风险分数
    triggered_capabilities: list[str] = field(default_factory=list)  # v0.11.0：命中规则时触发的当前能力


# ---------------------------------------------------------------------------
# v0.10.0 Capability-Based Permission Interaction Analyzer 配置类型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapabilityProducer:
    """工具调用 → 能力的产生条件。"""

    tool: str
    arg_match: dict[str, str] | None = None
    arg_not_match: dict[str, str] | None = None


@dataclass(frozen=True)
class CapabilityDef:
    """能力定义：一组产生条件，任一条件满足即产生该能力。"""

    name: str
    produced_by: list[CapabilityProducer]


@dataclass(frozen=True)
class CapabilityCombinationRule:
    """能力组合规则：历史能力 + 当前能力 → 风险标签与裁决建议。"""

    id: str
    description: str
    requires_any: list[str]
    triggers_any: list[str]
    action: Literal["deny", "require_approval"]
    reason: str
    risk_tags: list[str]
    score: int


@dataclass(frozen=True)
class CapabilityRules:
    """能力规则配置容器。"""

    capabilities: dict[str, CapabilityDef]
    combination_rules: list[CapabilityCombinationRule]


@dataclass(frozen=True)
class ValuePattern:
    name: str
    pattern: str
    replacement: str = "***"


@dataclass(frozen=True)
class MaskingRules:
    field_name_blacklist: list[str]
    value_patterns: list[ValuePattern]
    # v1.1（自审#1）分级掩码：视图 -> 应用的规则名列表。
    #   audit_log:       落盘审计日志应用全部规则（field_name_blacklist + value_patterns）；
    #   approval_request:审批视图只应用 field_name_blacklist（收件人/路径/正文须对审批人可见）。
    masking_applies_to: dict[str, list[str]]


@dataclass(frozen=True)
class ApprovalRule:
    """审批规则：按工具指定审批人（escalation_target）。"""

    tool_name: str
    approver: str


@dataclass(frozen=True)
class ApprovalConfig:
    default: str
    rules: list[ApprovalRule] = field(default_factory=list)


AuditHashAlgorithm = Literal["sha256", "hmac-sha256"]


@dataclass(frozen=True)
class AppConfig:
    agents: dict[str, Agent]
    users: dict[str, str]  # user_id -> display_name
    profiles: dict[str, CapabilityProfile]
    mcp_servers: dict[str, MCPServerConfig]
    tool_mapping: dict[str, ToolMappingEntry]
    http_tool_specs: dict[str, HTTPToolSpec]  # v0.21.0 HTTP 工具规格
    local_function_specs: dict[str, LocalFunctionSpec]  # v0.23.0 本地函数规格
    harness_tool_specs: dict[str, HarnessToolSpec]  # v0.25.0 Harness 工具规格
    harness_backends: dict[str, SubprocessBackendConfig | DockerBackendConfig | HTTPBackendConfig]  # v0.25.0 Harness 后端
    permission_rules: list[PermissionRule]
    capability_rules: CapabilityRules  # v0.10.0
    authority_rules: AuthorityRules  # v0.11.0
    audit_rules: AuditRules  # v0.12.0
    masking_rules: MaskingRules
    approval: ApprovalConfig
    policy_dir: str
    audit_log_path: str
    decision_log_path: str
    risk_state_path: str = "./data/risk_state.jsonl"  # v1.2 会话级风险状态持久化路径
    session_path: str = "./data/sessions.jsonl"  # v0.4.0 Session 持久化路径
    conversation_path: str = "./data/conversations.jsonl"  # v0.3.0 会话上下文持久化路径
    conversation_max_messages_per_session: int = 100  # v0.3.0 每个 session 保留消息数
    approval_store_path: str = "./data/approvals.jsonl"  # v0.3.0 审批请求/结果持久化路径
    task_store_path: str = "./data/tasks.jsonl"  # v0.6.0 Task 持久化路径
    budget_ledger_path: str = "./data/budget.jsonl"  # v0.6.0 预算事件持久化路径
    reservation_store_path: str = "./data/reservations.jsonl"  # v0.8.0 reservation 持久化路径
    authority_log_path: str = "./data/authority.jsonl"  # v0.11.0 authority token 持久化路径
    alert_store_path: str = "./data/alerts.jsonl"  # v0.12.0 alert/report 持久化路径
    llm_planner: LLMPlannerConfig | None = None
    audit_hash_algo: AuditHashAlgorithm = "sha256"
    audit_hmac_key_env: str = "LOOP_CONTROLLER_AUDIT_HMAC_KEY"
    audit_key_id: str = "default"  # HMAC key 标识，为密钥轮换留口
    identity_config: dict[str, Any] = field(default_factory=dict)  # v0.20.0 身份 Provider 配置
    entrypoints_config: dict[str, Any] = field(default_factory=dict)  # v0.20.0 入口认证配置
    secrets_config: dict[str, Any] = field(default_factory=dict)  # v0.22.0 Secret Broker 配置
    revocation_config: dict[str, Any] = field(default_factory=dict)  # v0.26.0 吊销配置
    evidence_config: dict[str, Any] = field(default_factory=dict)  # v0.26.0 证据链配置

# ---------------------------------------------------------------------------
# ConfigLoader
# ---------------------------------------------------------------------------


class ConfigLoader:
    """加载 config/ 目录并执行 7 条启动校验。"""

    def load(self, config_dir: str | Path, opa_base_url: str | None = None) -> AppConfig:
        """加载并校验配置.

        Args:
            config_dir: config/ 目录路径；同级根目录下的 policies/ 与 data/ 由此推导。
            opa_base_url: OPA HTTP 地址。为 None 时跳过校验 3（测试/无 OPA 场景）。

        Raises:
            ConfigValidationError: 任一启动校验失败。
        """
        config_dir = Path(config_dir)
        root = config_dir.parent

        agents, users = self._load_agents(config_dir / "agents.yaml")
        profiles = self._load_profiles(config_dir / "profiles.yaml")
        mcp_servers, tool_mapping, legacy_http_specs = self._load_mcp_servers(config_dir / "mcp_servers.yaml")
        http_tool_specs = self._load_http_tools(config_dir / "http_tools.yaml")
        # mcp_servers.yaml 中的 type: http 条目作为向后兼容补充
        http_tool_specs.update(legacy_http_specs)
        local_function_specs = self._load_local_functions(config_dir / "local_functions.yaml")
        harness_tool_specs, harness_backends = self._load_harness_tools(
            config_dir / "harness_tools.yaml"
        )
        secrets_config = self._load_secrets_config(config_dir / "secrets.yaml", root)
        revocation_config = self._load_optional_config(config_dir / "revocation.yaml")
        evidence_config = self._load_optional_config(config_dir / "evidence.yaml")
        approval = self._load_approval(config_dir / "approval.yaml")
        llm_planner = self._load_llm_planner(config_dir / "llm_planner.yaml")
        identity_config = self._load_identity_config(config_dir / "identity.yaml")
        entrypoints_config = self._load_entrypoints_config(config_dir / "entrypoints.yaml")
        permission_rules = self._load_permission_rules(config_dir / "permission_rules.yaml")
        capability_rules = self._load_capability_rules(config_dir / "capability_rules.yaml")
        authority_rules = self._load_authority_rules(config_dir / "authority_rules.yaml")
        audit_rules = self._load_audit_rules(config_dir / "audit_rules.yaml")
        masking_rules = self._load_masking_rules(config_dir / "masking_rules.yaml")

        audit_hash_algo = cast(
            AuditHashAlgorithm,
            os.environ.get("LOOP_CONTROLLER_AUDIT_HASH_ALGO", "hmac-sha256"),
        )
        if audit_hash_algo not in ("sha256", "hmac-sha256"):
            raise ConfigValidationError(
                f"环境变量 LOOP_CONTROLLER_AUDIT_HASH_ALGO 必须是 sha256 或 hmac-sha256，"
                f"当前值：{audit_hash_algo}"
            )

        audit_key_id = os.environ.get("LOOP_CONTROLLER_AUDIT_KEY_ID", "default").strip()
        if not audit_key_id:
            raise ConfigValidationError(
                "环境变量 LOOP_CONTROLLER_AUDIT_KEY_ID 不能为空（HMAC 模式下 key_id 用于轮换识别）"
            )

        conversation_path = os.environ.get(
            "LOOP_CONTROLLER_CONVERSATION_PATH", str(root / "data" / "conversations.jsonl")
        )
        approval_store_path = os.environ.get(
            "LOOP_CONTROLLER_APPROVAL_STORE_PATH", str(root / "data" / "approvals.jsonl")
        )
        session_path = os.environ.get(
            "LOOP_CONTROLLER_SESSION_PATH", str(root / "data" / "sessions.jsonl")
        )
        task_store_path = os.environ.get(
            "LOOP_CONTROLLER_TASK_STORE_PATH", str(root / "data" / "tasks.jsonl")
        )
        budget_ledger_path = os.environ.get(
            "LOOP_CONTROLLER_BUDGET_LEDGER_PATH", str(root / "data" / "budget.jsonl")
        )
        reservation_store_path = os.environ.get(
            "LOOP_CONTROLLER_RESERVATION_STORE_PATH", str(root / "data" / "reservations.jsonl")
        )

        app_config = AppConfig(
            agents=agents,
            users=users,
            profiles=profiles,
            mcp_servers=mcp_servers,
            tool_mapping=tool_mapping,
            http_tool_specs=http_tool_specs,
            local_function_specs=local_function_specs,
            harness_tool_specs=harness_tool_specs,
            harness_backends=harness_backends,
            permission_rules=permission_rules,
            capability_rules=capability_rules,
            authority_rules=authority_rules,
            audit_rules=audit_rules,
            masking_rules=masking_rules,
            approval=approval,
            policy_dir=str(root / "policies"),
            audit_log_path=str(root / "data" / "audit.jsonl"),
            decision_log_path=str(root / "data" / "decisions.jsonl"),
            risk_state_path=str(root / "data" / "risk_state.jsonl"),
            session_path=session_path,
            conversation_path=conversation_path,
            approval_store_path=approval_store_path,
            task_store_path=task_store_path,
            budget_ledger_path=budget_ledger_path,
            reservation_store_path=reservation_store_path,
            authority_log_path=str(root / "data" / "authority.jsonl"),
            alert_store_path=str(root / "data" / "alerts.jsonl"),
            llm_planner=llm_planner,
            audit_hash_algo=audit_hash_algo,
            audit_key_id=audit_key_id,
            identity_config=identity_config,
            entrypoints_config=entrypoints_config,
            secrets_config=secrets_config,
            revocation_config=revocation_config,
            evidence_config=evidence_config,
        )

        self._check_profile_exists(app_config)
        self._check_tool_mapping(app_config)
        if opa_base_url is not None:
            self._check_policy_loadable(opa_base_url, app_config)
        self._check_dirs_writable(app_config)
        self._check_glob_compile(app_config)
        self._check_regex_compile(app_config)
        self._check_approver_exists(app_config)
        self._check_llm_planner_api_key(app_config)
        self._check_audit_key(app_config)
        self._check_harness_config(app_config)
        self._check_identity_config(app_config)
        self._check_entrypoints_config(app_config)
        return app_config

    # -- 各 YAML 解析 -------------------------------------------------------

    def _load_agents(self, path: Path) -> tuple[dict[str, Agent], dict[str, str]]:
        data = self._read_yaml(path)
        agents: dict[str, Agent] = {}
        for item in data.get("agents", []):
            agent = Agent(**item)
            agents[agent.agent_id] = agent
        users: dict[str, str] = {}
        for item in data.get("users", []):
            users[item["user_id"]] = item.get("display_name", item["user_id"])
        return agents, users

    def _load_profiles(self, path: Path) -> dict[str, CapabilityProfile]:
        data = self._read_yaml(path)
        version = sha256(path.read_bytes()).hexdigest()[:12]
        profiles: dict[str, CapabilityProfile] = {}
        for item in data.get("profiles", []):
            tools_raw = item.pop("tools", {})
            tools: dict[str, ToolPermission] = {}
            for tool_name, perm in tools_raw.items():
                tools[tool_name] = ToolPermission(tool_name=tool_name, **perm)
            profile = CapabilityProfile(version=version, tools=tools, **item)
            profiles[profile.profile_id] = profile
        return profiles

    def _load_mcp_servers(
        self, path: Path
    ) -> tuple[dict[str, MCPServerConfig], dict[str, ToolMappingEntry], dict[str, HTTPToolSpec]]:
        data = self._read_yaml(path)
        servers: dict[str, MCPServerConfig] = {}
        for name, conf in data.get("servers", {}).items():
            servers[name] = MCPServerConfig(name=name, **conf)
        mapping: dict[str, ToolMappingEntry] = {}
        http_specs: dict[str, HTTPToolSpec] = {}
        for canonical, entry in data.get("tool_mapping", {}).items():
            entry_type = entry.get("type", "mcp")
            if entry_type == "http":
                # 解析 ${ENV} 引用，然后构造 HTTPToolSpec
                resolved = resolve_env_refs(entry)
                http_specs[canonical] = HTTPToolSpec(tool_name=canonical, **resolved)
            else:
                mapping[canonical] = ToolMappingEntry(**entry)
        return servers, mapping, http_specs

    def _load_http_tools(self, path: Path) -> dict[str, HTTPToolSpec]:
        """加载 HTTP 工具规格（v0.22.0 独立配置）。

        文件缺失时返回空 dict（向后兼容）。
        """
        specs: dict[str, HTTPToolSpec] = {}
        if not path.exists():
            return specs
        data = self._read_yaml(path)
        for canonical, entry in (data.get("tools") or {}).items():
            resolved = resolve_env_refs(entry)
            specs[canonical] = HTTPToolSpec(tool_name=canonical, **resolved)
        return specs

    def _load_secrets_config(
        self, path: Path, root: Path
    ) -> dict[str, Any]:
        """加载 Secret Broker 后端配置（v0.22.0）。

        文件缺失时使用默认文件后端：``<root>/secrets``。
        """
        if not path.exists():
            return {
                "backend": {
                    "type": "file",
                    "base_path": str(root / "secrets"),
                },
                "hot_reload": {"enabled": True, "poll_interval_seconds": 30},
            }
        data = self._read_yaml(path)
        config = cast(dict[str, Any], data)
        if "backend" not in config:
            config["backend"] = {"type": "file", "base_path": str(root / "secrets")}
        if "hot_reload" not in config:
            config["hot_reload"] = {"enabled": True, "poll_interval_seconds": 30}
        return config

    def _load_local_functions(self, path: Path) -> dict[str, LocalFunctionSpec]:
        """加载本地函数规格（v0.23.0）。

        文件缺失时返回空 dict（向后兼容）。
        """
        specs: dict[str, LocalFunctionSpec] = {}
        if not path.exists():
            return specs
        data = self._read_yaml(path)
        for canonical, entry in (data.get("tools") or {}).items():
            specs[canonical] = LocalFunctionSpec(tool_name=canonical, **entry)
        return specs

    def _load_harness_tools(
        self, path: Path
    ) -> tuple[
        dict[str, HarnessToolSpec],
        dict[str, SubprocessBackendConfig | DockerBackendConfig | HTTPBackendConfig],
    ]:
        """加载 Harness 后端与工具规格（v0.25.0）。

        文件缺失时返回空 dict（向后兼容）。
        """
        tool_specs: dict[str, HarnessToolSpec] = {}
        backends: dict[
            str, SubprocessBackendConfig | DockerBackendConfig | HTTPBackendConfig
        ] = {}
        if not path.exists():
            return tool_specs, backends
        data = self._read_yaml(path)
        for name, entry in (data.get("backends") or {}).items():
            backend_type = entry.get("type", "subprocess")
            try:
                if backend_type == "subprocess":
                    backends[name] = SubprocessBackendConfig(name=name, **entry)
                elif backend_type == "docker":
                    raise ConfigValidationError(
                        f"Harness 后端 {name} 使用不受支持的 docker 类型；请由部署层启动 HTTP Harness Service"
                    )
                elif backend_type == "http":
                    backends[name] = HTTPBackendConfig(name=name, **entry)
                else:
                    raise ConfigValidationError(
                        f"Harness 后端 {name} 的类型 {backend_type!r} 不受支持"
                    )
            except ValidationError as exc:
                raise ConfigValidationError(f"Harness 后端 {name} 配置非法：{exc}") from exc
        for canonical, entry in (data.get("tools") or {}).items():
            try:
                tool_specs[canonical] = HarnessToolSpec(tool_name=canonical, **entry)
            except ValidationError as exc:
                raise ConfigValidationError(f"Harness 工具 {canonical} 配置非法：{exc}") from exc
        return tool_specs, backends

    def reload_http_tools(self, config_dir: str | Path) -> dict[str, HTTPToolSpec]:
        """热更新：仅重新加载 HTTP 工具规格。"""
        config_dir = Path(config_dir)
        http_specs = self._load_http_tools(config_dir / "http_tools.yaml")
        mcp_path = config_dir / "mcp_servers.yaml"
        if mcp_path.exists():
            _, _, legacy_http_specs = self._load_mcp_servers(mcp_path)
            http_specs.update(legacy_http_specs)
        return http_specs

    def reload_secrets_config(
        self, config_dir: str | Path
    ) -> dict[str, Any]:
        """热更新：重新加载 secrets.yaml，返回最新配置。"""
        config_dir = Path(config_dir)
        root = config_dir.parent
        return self._load_secrets_config(config_dir / "secrets.yaml", root)

    def reload_revocation_config(self, config_dir: str | Path) -> dict[str, Any]:
        """热更新：重新加载 revocation.yaml。"""
        return self._load_optional_config(Path(config_dir) / "revocation.yaml")

    def _load_optional_config(self, path: Path) -> dict[str, Any]:
        """加载可选 YAML 配置；文件缺失时保持旧版本行为。"""
        if not path.exists():
            return {}
        return self._read_yaml(path)

    def _load_permission_rules(self, path: Path) -> list[PermissionRule]:
        data = self._read_yaml(path)
        rules: list[PermissionRule] = []
        for item in data.get("rules", []):
            conditions = [PermissionCondition(**c) for c in item.get("when_all", [])]
            rules.append(
                PermissionRule(
                    id=item["id"],
                    description=item.get("description", ""),
                    when_all=conditions,
                    action=item["action"],
                    reason=item.get("reason", ""),
                    risk_tags=item.get("risk_tags", []),
                    score=item.get("score", 0),
                )
            )
        return rules

    def _load_capability_rules(self, path: Path) -> CapabilityRules:
        """加载能力规则配置；文件缺失时返回空规则（向后兼容）。"""
        if not path.exists():
            return CapabilityRules(capabilities={}, combination_rules=[])
        data = self._read_yaml(path)
        capabilities: dict[str, CapabilityDef] = {}
        for name, cap in (data.get("capabilities") or {}).items():
            producers = [
                CapabilityProducer(
                    tool=p["tool"],
                    arg_match=p.get("arg_match"),
                    arg_not_match=p.get("arg_not_match"),
                )
                for p in cap.get("produced_by", [])
            ]
            capabilities[name] = CapabilityDef(name=name, produced_by=producers)
        combination_rules: list[CapabilityCombinationRule] = []
        for item in data.get("combination_rules", []):
            combination_rules.append(
                CapabilityCombinationRule(
                    id=item["id"],
                    description=item.get("description", ""),
                    requires_any=list(item.get("requires_any", [])),
                    triggers_any=list(item.get("triggers_any", [])),
                    action=item["action"],
                    reason=item.get("reason", ""),
                    risk_tags=list(item.get("risk_tags", [])),
                    score=item.get("score", 0),
                )
            )
        return CapabilityRules(capabilities=capabilities, combination_rules=combination_rules)

    def _load_authority_rules(self, path: Path) -> AuthorityRules:
        """加载动态权限规则配置；文件缺失时返回空规则（向后兼容）。"""
        if not path.exists():
            return AuthorityRules(enabled=False)
        data = self._read_yaml(path)
        grants: dict[str, AuthorityGrantRule] = {}
        for capability, item in (data.get("authority_grants") or {}).items():
            cond = item.get("conditions", {})
            grants[capability] = AuthorityGrantRule(
                capability=capability,
                description=item.get("description", ""),
                conditions=AuthorityConditions(
                    user_confirmation=cond.get("user_confirmation", False),
                    budget_remaining=cond.get("budget_remaining"),
                    no_recent_denials_within_steps=cond.get("no_recent_denials_within_steps"),
                    require_task_context_regex=cond.get("require_task_context_regex"),
                ),
                max_duration_seconds=item.get("max_duration_seconds", 300),
                budget_limit=BudgetCost(**item.get("budget_limit", {"token_count": 0})),
            )
        return AuthorityRules(
            enabled=data.get("enabled", True),
            grants=grants,
        )

    def _load_audit_rules(self, path: Path) -> AuditRules:
        """加载审计分析规则；文件缺失时返回空规则（向后兼容）。"""
        if not path.exists():
            return AuditRules(enabled=False)
        data = self._read_yaml(path)
        rules: list[AuditRule] = []
        for item in data.get("rules", []):
            cond = item.get("conditions", {})
            rules.append(
                AuditRule(
                    rule_id=item["id"],
                    description=item.get("description", ""),
                    severity=item.get("severity", "medium"),
                    conditions=AuditRuleConditions(
                        min_denies_count=cond.get("min_denies_count"),
                        min_denies_within_seconds=cond.get("min_denies_within_seconds"),
                        consecutive_denies=cond.get("consecutive_denies"),
                        action_sequence=cond.get("action_sequence"),
                        has_any_action=cond.get("has_any_action"),
                        has_all_actions=cond.get("has_all_actions"),
                        authority_token_exhausted=cond.get("authority_token_exhausted", False),
                    ),
                )
            )
        return AuditRules(enabled=data.get("enabled", True), rules=rules)

    def _load_masking_rules(self, path: Path) -> MaskingRules:
        data = self._read_yaml(path)
        patterns = [ValuePattern(**p) for p in data.get("value_patterns", [])]
        return MaskingRules(
            field_name_blacklist=data.get("field_name_blacklist", []),
            value_patterns=patterns,
            masking_applies_to=data.get("masking_applies_to", {}),
        )

    def _load_approval(self, path: Path) -> ApprovalConfig:
        data = self._read_yaml(path)
        rules = [ApprovalRule(**r) for r in data.get("rules", [])]
        return ApprovalConfig(default=data.get("approvers", {}).get("default", ""), rules=rules)

    def _load_llm_planner(self, path: Path) -> LLMPlannerConfig | None:
        """加载 LLMPlanner 配置；文件缺失时返回 None 以兼容旧配置树（测试用）。"""
        if not path.exists():
            return None
        data = self._read_yaml(path)
        return LLMPlannerConfig(**data)

    def _load_identity_config(self, path: Path) -> dict[str, Any]:
        """加载身份 Provider 配置；文件缺失返回空 dict（向后兼容）。"""
        if not path.exists():
            return {}
        data = self._read_yaml(path)
        return cast(dict[str, Any], data.get("identity", {}))

    def _load_entrypoints_config(self, path: Path) -> dict[str, Any]:
        """加载入口认证配置；文件缺失返回空 dict（向后兼容）。"""
        if not path.exists():
            return {}
        data = self._read_yaml(path)
        return cast(dict[str, Any], data.get("entrypoints", {}))

    # -- 7 条启动校验 -------------------------------------------------------

    def _check_profile_exists(self, config: AppConfig) -> None:
        for agent_id, agent in config.agents.items():
            if agent.profile_id not in config.profiles:
                raise ConfigValidationError(
                    f"Agent {agent_id} 引用的 profile_id {agent.profile_id} 不存在"
                )

    def _check_tool_mapping(self, config: AppConfig) -> None:
        all_tools = (
            set(config.tool_mapping)
            | set(config.http_tool_specs)
            | set(config.local_function_specs)
            | set(config.harness_tool_specs)
        )
        for profile_id, profile in config.profiles.items():
            for tool_name in profile.tools:
                if tool_name not in all_tools:
                    raise ConfigValidationError(
                        f"Profile {profile_id} 的工具 {tool_name} 不在 tool_mapping / http_tool_specs / local_function_specs / harness_tool_specs 中"
                    )

    def _check_policy_loadable(self, opa_base_url: str, config: AppConfig) -> None:
        policy_dir = Path(config.policy_dir)
        default_rego = policy_dir / "default.rego"
        if not default_rego.exists():
            raise ConfigValidationError(
                f"policy_dir {policy_dir} 下缺少 default.rego"
            )
        try:
            result = self._query_opa(opa_base_url, POLICY_PACKAGE, {})
        except Exception as exc:  # noqa: BLE001 - fail-closed 启动拒绝
            raise ConfigValidationError(f"OPA 试查询失败（{opa_base_url}）：{exc}") from exc
        decision = result.get("decision", {})
        if not isinstance(decision, dict) or decision.get("verdict") != "deny":
            raise ConfigValidationError(
                "OPA 试查询未返回结构合法的 deny（空 input 必须命中 default deny）"
            )

    @staticmethod
    def _query_opa(base_url: str, package: str, input_doc: dict[str, Any]) -> dict[str, Any]:
        """启动期同步 OPA 查询（仅配置校验使用；运行期查询在 policy_engine）.

        - URL 路径用 ``/`` 分隔包名：Rego 包 ``loop_controller.tool_permission``
          对应 ``/v1/data/loop_controller/tool_permission``（点号会被 OPA 当作
          字面路径段，导致查询不到）。
        - ``trust_env=False``：访问本地 OPA 必须绕过系统/环境代理，否则
          httpx 会经由代理返回 502。
        """
        url = f"{base_url.rstrip('/')}/v1/data/{package.replace('.', '/')}"
        resp = httpx.post(url, json={"input": input_doc}, timeout=2.0, trust_env=False)
        resp.raise_for_status()
        body = resp.json()
        result = body.get("result", {})
        if isinstance(result, dict) and "result" in result:
            return cast(dict[str, Any], result["result"])
        return cast(dict[str, Any], result)

    def _check_dirs_writable(self, config: AppConfig) -> None:
        for label, path_str in (
            ("audit_log", config.audit_log_path),
            ("decision_log", config.decision_log_path),
            ("risk_state", config.risk_state_path),
            ("conversation", config.conversation_path),
            ("approval_store", config.approval_store_path),
        ):
            path = Path(path_str)
            probe = path.parent / f".write_probe_{label}"
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                probe.write_text("", encoding="utf-8")
                probe.unlink()
            except OSError as exc:
                raise ConfigValidationError(
                    f"{label} 目录 {path.parent} 不可写：{exc}"
                ) from exc

    def _check_glob_compile(self, config: AppConfig) -> None:
        patterns: list[str] = []
        for profile in config.profiles.values():
            for perm in profile.tools.values():
                for values in list(perm.allowed_args.values()) + list(perm.denied_args.values()):
                    patterns.extend(values)
        for rule in config.permission_rules:
            for cond in rule.when_all:
                if cond.history_arg_match:
                    patterns.extend(cond.history_arg_match.values())
                if cond.current_arg_not_match:
                    patterns.extend(cond.current_arg_not_match.values())
        for cap in config.capability_rules.capabilities.values():
            for producer in cap.produced_by:
                if producer.arg_match:
                    patterns.extend(producer.arg_match.values())
                if producer.arg_not_match:
                    patterns.extend(producer.arg_not_match.values())
        for pattern in patterns:
            try:
                compile_glob(pattern)
            except ValueError as exc:
                raise ConfigValidationError(f"非法 glob 模式 {pattern!r}：{exc}") from exc

    def _check_regex_compile(self, config: AppConfig) -> None:
        for vp in config.masking_rules.value_patterns:
            try:
                re.compile(vp.pattern)
            except re.error as exc:
                raise ConfigValidationError(
                    f"非法掩码正则 {vp.pattern!r}：{exc}"
                ) from exc
        for rule in config.authority_rules.grants.values():
            if rule.conditions.require_task_context_regex:
                try:
                    re.compile(rule.conditions.require_task_context_regex)
                except re.error as exc:
                    raise ConfigValidationError(
                        f"非法 authority 正则 {rule.conditions.require_task_context_regex!r}：{exc}"
                    ) from exc

    def _check_approver_exists(self, config: AppConfig) -> None:
        """v1.1（评审#9）校验 7：approver 存在于 users 且不等于任何 agent_id。"""
        approvers = [config.approval.default] + [r.approver for r in config.approval.rules]
        for approver in approvers:
            if approver and approver not in config.users:
                raise ConfigValidationError(f"审批人 {approver} 不存在于 users 中")
            if approver and approver in config.agents:
                raise ConfigValidationError(
                    f"审批人 {approver} 是 Agent 身份，不能作为审批人（approver != agent_id）"
                )

    def _check_llm_planner_api_key(self, config: AppConfig) -> None:
        """T3.5 启动校验：LLMPlanner 启用时，api_key_env 指向的环境变量必须存在。"""
        if config.llm_planner is None or not config.llm_planner.enabled:
            return
        env_name = config.llm_planner.api_key_env
        if not os.environ.get(env_name):
            raise ConfigValidationError(
                f"LLMPlanner 已启用，但环境变量 {env_name} 未设置（密钥不落盘，请通过环境变量注入）"
            )

    def _check_audit_key(self, config: AppConfig) -> None:
        """P0 启动校验：HMAC 模式下环境变量必须存在且能解析为 ≥32 字节随机 key。"""
        if config.audit_hash_algo != "hmac-sha256":
            return
        try:
            self.resolve_audit_key(config)
        except ValueError as exc:
            raise ConfigValidationError(
                f"audit_hash_algo=hmac-sha256 时，{config.audit_hmac_key_env} 未设置或格式非法：{exc}"
            ) from exc

    @staticmethod
    def resolve_audit_key(config: AppConfig) -> bytes:
        """解析 ``audit_hmac_key_env`` 指向的环境变量为 bytes key。

        支持 hex（64 字符）或 base64 编码；解码后长度必须 ≥32 字节。
        返回 bytes；解析失败抛 ``ValueError``。
        """
        env_name = config.audit_hmac_key_env
        raw = os.environ.get(env_name)
        if not raw:
            raise ValueError(f"环境变量 {env_name} 未设置")
        raw = raw.strip()
        if len(raw) == 64:
            try:
                key = bytes.fromhex(raw)
                if len(key) >= 32:
                    return key
            except ValueError:
                pass
        try:
            key = base64.b64decode(raw, validate=True)
        except binascii.Error as exc:
            raise ValueError(f"无法解析为 hex 或 base64：{exc}") from exc
        if len(key) < 32:
            raise ValueError(f"key 长度 {len(key)} 字节，必须 ≥32 字节")
        return key

    def _check_harness_config(self, config: AppConfig) -> None:
        """校验 Harness backend/tool 引用、传输与认证边界。"""
        for tool_name, spec in config.harness_tool_specs.items():
            if spec.harness not in config.harness_backends:
                raise ConfigValidationError(
                    f"Harness 工具 {tool_name} 引用的 backend {spec.harness} 不存在"
                )
            try:
                json.dumps(spec.input_schema)
            except (TypeError, ValueError) as exc:
                raise ConfigValidationError(
                    f"Harness 工具 {tool_name} 的 input_schema 不是合法 JSON Schema 对象"
                ) from exc
            schema_type = spec.input_schema.get("type")
            if schema_type is not None and schema_type not in {
                "array", "boolean", "integer", "null", "number", "object", "string"
            }:
                raise ConfigValidationError(
                    f"Harness 工具 {tool_name} 的 input_schema.type 非法：{schema_type!r}"
                )

        for name, backend in config.harness_backends.items():
            if not isinstance(backend, HTTPBackendConfig):
                continue
            parsed = urlparse(backend.base_url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ConfigValidationError(
                    f"Harness HTTP 后端 {name} 的 base_url 必须是有效的 HTTP(S) URL"
                )
            loopback = parsed.hostname in {"localhost", "127.0.0.1"}
            if parsed.scheme == "http" and not (loopback and backend.allow_insecure_http):
                raise ConfigValidationError(
                    f"Harness HTTP 后端 {name} 必须使用 HTTPS；仅 loopback 可显式设置 allow_insecure_http"
                )
            if parsed.scheme == "https" and not backend.tls.verify:
                raise ConfigValidationError(
                    f"Harness HTTP 后端 {name} 的生产 HTTPS 不得关闭 TLS 校验"
                )
            if backend.auth.type != "none":
                env_name = backend.auth.key_env
                if not env_name or not os.environ.get(env_name, "").strip():
                    raise ConfigValidationError(
                        f"Harness HTTP 后端 {name} 的认证环境变量 {env_name!r} 未设置或为空"
                    )
            for field_name in ("ca_file", "client_cert_file", "client_key_file"):
                file_name = getattr(backend.tls, field_name)
                if not file_name:
                    continue
                path = Path(file_name)
                if not path.is_file() or not os.access(path, os.R_OK):
                    raise ConfigValidationError(
                        f"Harness HTTP 后端 {name} 的 TLS 文件 {field_name} 不存在或不可读"
                    )

    def _check_identity_config(self, config: AppConfig) -> None:
        """校验 identity provider 配置，避免启动后因配置错误才发现问题。"""
        identity = config.identity_config
        if not identity:
            return
        provider = identity.get("provider", "static")
        if provider not in {"static", "jwt", "mtls"}:
            raise ConfigValidationError(
                f"identity.provider 必须是 static / jwt / mtls 之一，当前值：{provider!r}"
            )

        if provider == "jwt":
            jwt_cfg = identity.get("jwt", {})
            if not jwt_cfg.get("issuer"):
                raise ConfigValidationError("identity.provider=jwt 时必须配置 jwt.issuer")
            if not jwt_cfg.get("jwks_url") and not jwt_cfg.get("public_key"):
                raise ConfigValidationError(
                    "identity.provider=jwt 时必须配置 jwt.jwks_url 或 jwt.public_key"
                )

        if provider == "mtls":
            mtls_cfg = identity.get("mtls", {})
            if not mtls_cfg.get("cert_subject_template") and not mtls_cfg.get("cert_mappings"):
                raise ConfigValidationError(
                    "identity.provider=mtls 时必须配置 mtls.cert_subject_template 或 mtls.cert_mappings"
                )

        if provider == "static":
            static_cfg = identity.get("static", {})
            tokens = static_cfg.get("allowed_tokens", [])
            if not isinstance(tokens, list):
                raise ConfigValidationError("identity.static.allowed_tokens 必须是列表")
            for idx, entry in enumerate(tokens):
                if not isinstance(entry, dict):
                    raise ConfigValidationError(
                        f"identity.static.allowed_tokens[{idx}] 必须是对象"
                    )
                for field in ("token", "agent_id", "user_id"):
                    if not entry.get(field):
                        raise ConfigValidationError(
                            f"identity.static.allowed_tokens[{idx}] 缺少或空字段 {field}"
                        )

    def _check_entrypoints_config(self, config: AppConfig) -> None:
        """校验入口认证配置，避免未知的 auth 类型或格式错误。"""
        entrypoints = config.entrypoints_config
        if not entrypoints:
            return
        allowed_auths = {"jwt", "mtls", "static_token", "none"}
        for name, cfg in entrypoints.items():
            if not isinstance(cfg, dict):
                raise ConfigValidationError(f"entrypoints.{name} 必须是对象")
            auth = cfg.get("auth")
            if auth is not None and auth not in allowed_auths:
                raise ConfigValidationError(
                    f"entrypoints.{name}.auth 必须是 {allowed_auths} 之一，当前值：{auth!r}"
                )
            require_auth = cfg.get("require_auth")
            if require_auth is not None and not isinstance(require_auth, bool):
                raise ConfigValidationError(
                    f"entrypoints.{name}.require_auth 必须是布尔值"
                )
            admin_agent_ids = cfg.get("admin_agent_ids")
            if admin_agent_ids is not None and (
                name != "grpc"
                or not isinstance(admin_agent_ids, list)
                or any(not isinstance(agent_id, str) or not agent_id for agent_id in admin_agent_ids)
            ):
                raise ConfigValidationError(
                    "entrypoints.grpc.admin_agent_ids 必须是非空字符串列表"
                )

    # -- 工具 ---------------------------------------------------------------

    @staticmethod
    def _read_yaml(path: Path) -> dict[str, Any]:
        if not path.exists():
            raise ConfigValidationError(f"配置文件缺失：{path}")

        class UniqueKeyLoader(yaml.SafeLoader):
            pass

        def construct_unique_mapping(
            loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False
        ) -> dict[Any, Any]:
            mapping: dict[Any, Any] = {}
            for key_node, value_node in node.value:
                key = loader.construct_object(key_node, deep=deep)
                if key in mapping:
                    raise ConfigValidationError(
                        f"配置文件 {path} 包含重复名称 {key!r}"
                    )
                mapping[key] = loader.construct_object(value_node, deep=deep)
            return mapping

        UniqueKeyLoader.add_constructor(
            yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
            construct_unique_mapping,
        )
        with path.open("r", encoding="utf-8") as f:
            data = yaml.load(f, Loader=UniqueKeyLoader)
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise ConfigValidationError(f"配置文件格式错误（应为 mapping）：{path}")
        return data
