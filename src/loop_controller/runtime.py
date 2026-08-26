"""Runtime 组装：R1/R2/R3 运行时依赖容器（v0.13.1）。

``Runtime`` 是内部依赖容器，供 ``LoopController`` 使用。核心产品 API 是
``loop_controller.controller.LoopController``，Agent 自己掌握主循环，只在每次
调工具时调用 ``LoopController.evaluate_and_execute`` / ``resume_after_approval``。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loop_controller.approval_manager import AsyncApprovalManager
from loop_controller.audit_analyzer import AuditAnalyzer, RuleBasedAuditAnalyzer
from loop_controller.authority import EarnedAuthorityManager
from loop_controller.budget import JsonlBudgetLedger
from loop_controller.checkpoint import Checkpoint
from loop_controller.classifier import LightweightClassifier, RuleBasedClassifier
from loop_controller.executors import ExecutorRegistry, LocalFunctionExecutor, MCPExecutor
from loop_controller.executors.http_client import HTTPClient
from loop_controller.executors.http_executor import HTTPExecutor
from loop_controller.identity import ConfigIdentityProvider, IdentityProvider
from loop_controller.infra.alert_store import JsonlAlertStore
from loop_controller.infra.approval_store import JsonlApprovalStore
from loop_controller.infra.audit_store import AuditStore, JsonlAuditStore
from loop_controller.infra.authority_store import JsonlAuthorityStore
from loop_controller.infra.config_loader import AppConfig, ConfigLoader
from loop_controller.infra.conversation_store import (
    ConversationContext,
    ConversationMessage,
    JsonlConversationStore,
)
from loop_controller.infra.decision_store import JsonlDecisionStore
from loop_controller.infra.hot_reload import HotReloader
from loop_controller.infra.policy_store import FilePolicyStore
from loop_controller.infra.reservation_store import (
    InMemoryReservationStore,
    JsonlReservationStore,
    ReservationStore,
)
from loop_controller.infra.task_store import InMemoryTaskStore, JsonlTaskStore, TaskStore
from loop_controller.masker import Masker
from loop_controller.mcp_gateway import MCPGateway
from loop_controller.models import BudgetCost, Task
from loop_controller.permission_interaction import (
    CapabilityBasedPermissionAnalyzer,
    CompositePermissionInteractionAnalyzer,
    ConfigPermissionInteractionAnalyzer,
)
from loop_controller.policy_engine import OPAPolicyEngine
from loop_controller.risk_state import JsonlRiskStateStore, RiskStateManager
from loop_controller.secrets import FileSecretBackend, MemorySecretBackend, SecretBroker
from loop_controller.session import JsonlSessionBackend, Session, SessionManager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Runtime：运行时依赖容器
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Runtime:
    """R1/R2/R3 运行时依赖容器（v0.13.1）。"""

    classifier: LightweightClassifier
    checkpoint: Checkpoint
    gateway: MCPGateway
    approval_manager: AsyncApprovalManager
    audit_store: AuditStore
    masker: Masker
    profiles: dict[str, Any]  # CapabilityProfile
    session_manager: SessionManager
    risk_manager: RiskStateManager
    conversation_store: JsonlConversationStore
    task_store: TaskStore = field(default_factory=InMemoryTaskStore)
    reservation_store: ReservationStore = field(default_factory=InMemoryReservationStore)
    audit_analyzer: AuditAnalyzer | None = None
    http_client: HTTPClient | None = None  # v0.21.0
    http_tool_names: set[str] = field(default_factory=set)  # v0.21.0
    secret_broker: SecretBroker | None = None  # v0.22.0
    hot_reloader: HotReloader | None = None  # v0.22.0

    def create_task(
        self,
        user_id: str,
        agent_id: str,
        description: str,
        session_id: str | None = None,
    ) -> tuple[Task, Session]:
        """通过 SessionManager 分配/复用 session，构造 Task（v1.2/v0.4.0 推荐入口）。"""
        if session_id is not None:
            session = self.session_manager.get_session(session_id)
            if session is None or self.session_manager.is_session_expired(session_id):
                raise ValueError(f"session {session_id} not found or expired")
            if session.user_id != user_id:
                raise ValueError(
                    f"session {session_id} user_id mismatch: {session.user_id} != {user_id}"
                )
            session = self.session_manager.touch_session(session_id)
        else:
            session = self.session_manager.get_or_create_session(
                user_id=user_id,
                agent_id=agent_id,
            )
        task = Task(
            task_id=uuid.uuid4().hex,
            session_id=session.session_id,
            user_id=user_id,
            agent_id=agent_id,
            description=description,
        )
        self.task_store.save(task)
        self.add_user_message(session.session_id, task.task_id, description)
        return task, session

    def get_task(self, task_id: str) -> Task | None:
        """按 task_id 取 Task。"""
        return self.task_store.get(task_id)

    def add_user_message(
        self, session_id: str, task_id: str, content: str
    ) -> ConversationMessage:
        """写入用户消息并返回。"""
        message = ConversationMessage(
            message_id=uuid.uuid4().hex,
            session_id=session_id,
            role="user",
            content=content,
            task_id=task_id,
        )
        self.conversation_store.append_message(message)
        return message

    def add_agent_message(
        self, session_id: str, task_id: str, content: str
    ) -> ConversationMessage:
        """写入 Agent 消息并返回。"""
        message = ConversationMessage(
            message_id=uuid.uuid4().hex,
            session_id=session_id,
            role="agent",
            content=content,
            task_id=task_id,
        )
        self.conversation_store.append_message(message)
        return message

    def get_conversation_context(self, session_id: str) -> ConversationContext:
        """取会话上下文供治理使用。"""
        return self.conversation_store.get_context(session_id)

    async def start(self) -> None:
        """拉起 MCP gateway 等异步初始化。"""
        await self.gateway.start()
        if self.http_client is not None:
            await self.http_client.start()
        if self.hot_reloader is not None:
            await self.hot_reloader.start()

    async def aclose(self) -> None:
        """关闭 MCP gateway 等异步资源。"""
        if self.hot_reloader is not None:
            await self.hot_reloader.stop()
        await self.gateway.aclose()
        if self.http_client is not None:
            await self.http_client.aclose()


# ---------------------------------------------------------------------------
# Identity 工厂
# ---------------------------------------------------------------------------


def _build_identity_provider(config: AppConfig) -> IdentityProvider:
    """根据 config.identity_config 构造对应 IdentityProvider。"""
    provider_type = config.identity_config.get("provider", "static")
    agents = config.agents
    users = config.users

    if provider_type == "jwt":
        jwt_cfg = config.identity_config.get("jwt", {})
        from loop_controller.identity.jwt import JWTIdentityProvider

        return JWTIdentityProvider(
            agents=agents,
            users=users,
            issuer=jwt_cfg["issuer"],
            audience=jwt_cfg.get("audience", "loop-controller"),
            jwks_url=jwt_cfg.get("jwks_url"),
            public_key=jwt_cfg.get("public_key"),
            claim_mappings=jwt_cfg.get("claim_mappings"),
        )

    if provider_type == "mtls":
        mtls_cfg = config.identity_config.get("mtls", {})
        from loop_controller.identity.mtls import MTLSIdentityProvider

        return MTLSIdentityProvider(
            agents=agents,
            users=users,
            cert_mappings=mtls_cfg.get("cert_mappings"),
            cert_subject_template=mtls_cfg.get("cert_subject_template"),
        )

    # static / default
    static_cfg = config.identity_config.get("static", {})
    return ConfigIdentityProvider(
        agents=agents,
        users=users,
        allowed_tokens=static_cfg.get("allowed_tokens"),
    )


# ---------------------------------------------------------------------------
# Secret Broker 工厂
# ---------------------------------------------------------------------------


def _build_secret_broker(config: AppConfig) -> SecretBroker:
    """根据 ``config.secrets_config`` 构造 Secret Broker。"""
    backend_config = config.secrets_config.get("backend", {})
    backend_type = backend_config.get("type", "file")
    if backend_type == "memory":
        return MemorySecretBackend()
    base_path = backend_config.get("base_path")
    if not base_path:
        project_root = Path(config.policy_dir).parent
        base_path = str(project_root / "secrets")
    return FileSecretBackend(base_path)


# ---------------------------------------------------------------------------
# Runtime 工厂
# ---------------------------------------------------------------------------


def build_runtime(
    config: AppConfig,
    *,
    opa_url: str = "http://127.0.0.1:8181",
    env_extra: dict[str, str] | None = None,
) -> Runtime:
    """从 ``AppConfig`` 组装 Runtime。

    Args:
        config: 经 ``ConfigLoader.load`` 加载并校验后的配置。
        opa_url: OPA sidecar HTTP 地址。
        env_extra: 传递给 MCP 子进程的额外环境变量；默认会注入 ``PYTHONPATH`` 指向项目 ``src``。
    """
    identity = _build_identity_provider(config)
    policy_store = FilePolicyStore(config.policy_dir)
    policy_engine = OPAPolicyEngine(base_url=opa_url, timeout=2.0)

    project_root = Path(config.policy_dir).parent
    mcp_env = {"PYTHONPATH": str(project_root / "src")}
    if env_extra is not None:
        mcp_env.update(env_extra)
    gateway = MCPGateway(
        mcp_servers=dict(config.mcp_servers),
        tool_mapping=config.tool_mapping,
        env_extra=mcp_env,
        cwd=str(project_root),
    )
    mcp_executor = MCPExecutor(gateway)
    secret_broker = _build_secret_broker(config)
    http_client = HTTPClient()
    http_executor = HTTPExecutor(
        http_client, config.http_tool_specs, secret_broker=secret_broker
    )
    local_executor = LocalFunctionExecutor(config.local_function_specs)
    executor_registry = ExecutorRegistry()
    for canonical_name in config.tool_mapping:
        executor_registry.register(canonical_name, mcp_executor)
    for canonical_name in config.http_tool_specs:
        executor_registry.register(canonical_name, http_executor)
    for canonical_name in config.local_function_specs:
        executor_registry.register(canonical_name, local_executor)
    masker = Masker(config.masking_rules)
    budget_ledger = JsonlBudgetLedger(config.budget_ledger_path)
    session_manager = SessionManager(backend=JsonlSessionBackend(config.session_path))
    risk_manager = RiskStateManager(JsonlRiskStateStore(config.risk_state_path))
    conversation_store = JsonlConversationStore(
        config.conversation_path,
        max_messages_per_session=config.conversation_max_messages_per_session,
    )
    task_store = JsonlTaskStore(config.task_store_path)
    reservation_store = JsonlReservationStore(config.reservation_store_path)
    authority_manager = EarnedAuthorityManager(
        rules=config.authority_rules,
        store=JsonlAuthorityStore(config.authority_log_path),
    )
    checkpoint = Checkpoint(
        profiles=config.profiles,
        policy_engine=policy_engine,
        policy_store=policy_store,
        gateway=gateway,
        executor_registry=executor_registry,
        identity=identity,
        session_manager=session_manager,
        risk_manager=risk_manager,
        decision_store=JsonlDecisionStore(config.decision_log_path),
        budget_ledger=budget_ledger,
        reservation_store=reservation_store,
        permission_analyzer=CompositePermissionInteractionAnalyzer(
            ConfigPermissionInteractionAnalyzer(config.permission_rules),
            CapabilityBasedPermissionAnalyzer(config.capability_rules),
        ),
        authority_manager=authority_manager,
        tool_costs={
            **{
                name: BudgetCost(token_count=entry.cost_per_call)
                for name, entry in config.tool_mapping.items()
            },
            **{
                name: BudgetCost(token_count=spec.cost_per_call)
                for name, spec in config.http_tool_specs.items()
            },
            **{
                name: BudgetCost(token_count=spec.cost_per_call)
                for name, spec in config.local_function_specs.items()
            },
        },
        masker=masker,
    )
    audit_key: bytes | None = None
    if config.audit_hash_algo == "hmac-sha256":
        audit_key = ConfigLoader.resolve_audit_key(config)
    audit_store = JsonlAuditStore(
        config.audit_log_path,
        hash_algo=config.audit_hash_algo,
        hmac_key=audit_key,
        key_id=config.audit_key_id,
    )
    approval_manager = AsyncApprovalManager(
        JsonlApprovalStore(config.approval_store_path)
    )
    alert_store = JsonlAlertStore(config.alert_store_path)
    audit_analyzer = RuleBasedAuditAnalyzer(
        rules=config.audit_rules,
        audit_store=audit_store,
        alert_store=alert_store,
    )

    hot_reload_config = config.secrets_config.get("hot_reload", {})
    config_dir = Path(config.policy_dir).parent / "config"
    # 与 Runtime 共享同一可变集合，确保 HTTP 工具热更新后 controller 可见。
    http_tool_names = set(config.http_tool_specs)
    hot_reloader = HotReloader(
        config_dir=config_dir,
        config_loader=ConfigLoader(),
        http_executor=http_executor,
        secret_broker=secret_broker,
        http_tool_names=http_tool_names,
        enabled=hot_reload_config.get("enabled", True),
        poll_interval_seconds=hot_reload_config.get("poll_interval_seconds", 30),
    )

    return Runtime(
        classifier=RuleBasedClassifier(),
        checkpoint=checkpoint,
        gateway=gateway,
        approval_manager=approval_manager,
        audit_store=audit_store,
        masker=masker,
        profiles=config.profiles,
        session_manager=session_manager,
        risk_manager=risk_manager,
        conversation_store=conversation_store,
        task_store=task_store,
        reservation_store=reservation_store,
        audit_analyzer=audit_analyzer,
        http_client=http_client,
        http_tool_names=http_tool_names,
        secret_broker=secret_broker,
        hot_reloader=hot_reloader,
    )
