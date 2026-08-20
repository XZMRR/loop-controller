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
import os
import re
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, cast

import httpx
import yaml

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
        mcp_servers, tool_mapping = self._load_mcp_servers(config_dir / "mcp_servers.yaml")
        approval = self._load_approval(config_dir / "approval.yaml")
        llm_planner = self._load_llm_planner(config_dir / "llm_planner.yaml")
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
    ) -> tuple[dict[str, MCPServerConfig], dict[str, ToolMappingEntry]]:
        data = self._read_yaml(path)
        servers: dict[str, MCPServerConfig] = {}
        for name, conf in data.get("servers", {}).items():
            servers[name] = MCPServerConfig(name=name, **conf)
        mapping: dict[str, ToolMappingEntry] = {}
        for canonical, entry in data.get("tool_mapping", {}).items():
            mapping[canonical] = ToolMappingEntry(**entry)
        return servers, mapping

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

    # -- 7 条启动校验 -------------------------------------------------------

    def _check_profile_exists(self, config: AppConfig) -> None:
        for agent_id, agent in config.agents.items():
            if agent.profile_id not in config.profiles:
                raise ConfigValidationError(
                    f"Agent {agent_id} 引用的 profile_id {agent.profile_id} 不存在"
                )

    def _check_tool_mapping(self, config: AppConfig) -> None:
        for profile_id, profile in config.profiles.items():
            for tool_name in profile.tools:
                if tool_name not in config.tool_mapping:
                    raise ConfigValidationError(
                        f"Profile {profile_id} 的工具 {tool_name} 不在 tool_mapping 中"
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

    # -- 工具 ---------------------------------------------------------------

    @staticmethod
    def _read_yaml(path: Path) -> dict[str, Any]:
        if not path.exists():
            raise ConfigValidationError(f"配置文件缺失：{path}")
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise ConfigValidationError(f"配置文件格式错误（应为 mapping）：{path}")
        return data
