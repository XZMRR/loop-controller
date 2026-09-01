"""Runtime 组装：R1/R2/R3 运行时依赖容器（v0.13.1）。

``Runtime`` 是内部依赖容器，供 ``LoopController`` 使用。核心产品 API 是
``loop_controller.controller.LoopController``，Agent 自己掌握主循环，只在每次
调工具时调用 ``LoopController.evaluate_and_execute`` / ``resume_after_approval``。
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loop_controller.approval_manager import AsyncApprovalManager
from loop_controller.audit.anchor_backends import HTTPAnchorBackend, HTTPAnchorConfig
from loop_controller.audit.anchors import AnchorReceiptVerifier
from loop_controller.audit.evidence import Ed25519EvidenceSigner, EvidenceChain, HMACEvidenceSigner
from loop_controller.audit.evidence_backends import LocalFileEvidenceBackend
from loop_controller.audit_analyzer import AuditAnalyzer, RuleBasedAuditAnalyzer
from loop_controller.authority import EarnedAuthorityManager
from loop_controller.budget import JsonlBudgetLedger
from loop_controller.checkpoint import Checkpoint, DecisionStore
from loop_controller.classifier import LightweightClassifier, RuleBasedClassifier
from loop_controller.executors import (
    ExecutorRegistry,
    HarnessExecutor,
    LocalFunctionExecutor,
    MCPExecutor,
)
from loop_controller.executors.http_client import HTTPClient
from loop_controller.executors.http_executor import HTTPExecutor
from loop_controller.go_kernel_bridge import AgentCard, AgentEntrypoint, GoKernelBridge
from loop_controller.identity import ConfigIdentityProvider, IdentityProvider
from loop_controller.identity.revocation import RevocationList
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
from loop_controller.infra.persistence_probe import (
    PersistenceProbe,
    PersistenceStatus,
    PersistenceTarget,
)
from loop_controller.infra.policy_store import FilePolicyStore
from loop_controller.infra.reservation_store import (
    InMemoryReservationStore,
    JsonlReservationStore,
    ReservationStore,
)
from loop_controller.infra.sqlite_decision_store import SqliteDecisionStore
from loop_controller.infra.sqlite_risk_state_store import SqliteRiskStateStore
from loop_controller.infra.state_db import StateDatabase
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
from loop_controller.risk_state import JsonlRiskStateStore, RiskStateManager, RiskStateStore
from loop_controller.secrets import (
    EncryptedFileSecretBackend,
    FileSecretBackend,
    MemorySecretBackend,
    SecretBroker,
)
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
    harness_executor: HarnessExecutor | None = None  # v0.25.0
    revocation_list: RevocationList = field(default_factory=RevocationList)  # v0.26.0
    evidence_anchor: HTTPAnchorBackend | None = None  # v0.28.0
    persistence_status: PersistenceStatus = field(default_factory=PersistenceStatus)
    go_kernel_bridge: GoKernelBridge | None = None  # v0.36.0 A2A 交互治理桥接
    local_agent_config: dict[str, Any] = field(default_factory=dict)

    def require_execution_ready(self) -> None:
        if self.persistence_status.status not in {"healthy", "tail_repaired"}:
            raise RuntimeError(
                f"持久化状态 {self.persistence_status.status} 不允许执行工具"
            )
        if isinstance(self.audit_store, JsonlAuditStore) and self.audit_store.write_blocked:
            raise RuntimeError("审计完整性状态不允许执行工具")

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
            session = self.session_manager.touch_session(
                session_id,
                user_id=user_id,
                agent_id=agent_id,
            )
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

    def add_user_message(self, session_id: str, task_id: str, content: str) -> ConversationMessage:
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

    def add_agent_message(self, session_id: str, task_id: str, content: str) -> ConversationMessage:
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
        """完成本地/远端完整性验证后，按锚点状态决定是否拉起执行面。"""
        persistence = getattr(self, "persistence_status", None)
        if persistence is not None and persistence.status in {
            "write_blocked",
            "lock_unavailable",
            "degraded",
        }:
            return
        if isinstance(self.audit_store, JsonlAuditStore):
            local_valid = await self.audit_store.verify_evidence_chain()
            if local_valid:
                await self.audit_store.verify_anchor_startup()
            if self.audit_store.write_blocked:
                return
        # v0.29.0：锚点验证通过后清理过期预算预留，防止崩溃/超时导致预算永久占用。
        self.checkpoint.recover_stale_reservations()
        await self.gateway.start()
        if self.http_client is not None:
            await self.http_client.start()
        if self.hot_reloader is not None:
            await self.hot_reloader.start()
        if self.harness_executor is not None:
            await self.harness_executor.start()
        if self.go_kernel_bridge is not None:
            await self._register_local_agent_card()

    async def _register_local_agent_card(self) -> None:
        """向 Go 内核注册本地 Agent Card（v0.36.0）。"""
        try:
            local = self.local_agent_config
            entrypoint = local.get("entrypoint", "http://127.0.0.1:8000")
            capabilities = local.get("capabilities", ["delegate_execution"])
            await self.go_kernel_bridge.register_agent(
                AgentCard(
                    agent_id=local.get("agent_id", "loop-controller-local"),
                    name=local.get("name", "Loop Controller Python Runtime"),
                    entrypoint=AgentEntrypoint("http", entrypoint),
                    capabilities=capabilities if isinstance(capabilities, list) else ["delegate_execution"],
                    version=local.get("version", "0.36.0"),
                )
            )
        except Exception as exc:
            logger.warning("Failed to register local Agent Card with Go kernel: %s", exc)

    async def aclose(self) -> None:
        """关闭 MCP gateway 等异步资源。"""
        if self.hot_reloader is not None:
            await self.hot_reloader.stop()
        if self.harness_executor is not None:
            await self.harness_executor.stop()
        await self.gateway.aclose()
        if self.http_client is not None:
            await self.http_client.aclose()
        if self.evidence_anchor is not None:
            self.evidence_anchor.close()
        if self.go_kernel_bridge is not None:
            await self.go_kernel_bridge.aclose()


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


def _build_evidence_chain(config: AppConfig) -> EvidenceChain | None:
    """按可选 evidence.yaml 构造本地签名证据链。"""
    evidence = config.evidence_config.get("evidence")
    if not evidence or not evidence.get("enabled", False):
        return None
    if evidence.get("backend", "local") != "local":
        raise ValueError("evidence.backend 当前仅支持 local")
    signing = evidence.get("signing", {})
    algorithm = signing.get("algorithm", "hmac-sha256")
    key_id = signing.get("key_id", config.audit_key_id)
    signer: Ed25519EvidenceSigner | HMACEvidenceSigner
    if algorithm == "ed25519":
        signer = Ed25519EvidenceSigner.from_environment(
            key_id=key_id,
            variable=signing.get("private_key_env", "LOOP_CONTROLLER_EVIDENCE_PRIVATE_KEY"),
        )
    elif algorithm == "hmac-sha256":
        key_env = signing.get("key_env", "LOOP_CONTROLLER_EVIDENCE_HMAC_KEY")
        encoded = os.environ.get(key_env)
        if not encoded:
            raise ValueError(f"环境变量 {key_env} 未配置")
        signer = HMACEvidenceSigner(encoded.encode("utf-8"), key_id=key_id)
    else:
        raise ValueError(f"不支持的证据签名算法：{algorithm}")
    local_config = evidence.get("local", {})
    local_path = local_config.get("path", "evidence")
    path = Path(local_path)
    if not path.is_absolute():
        path = Path(config.policy_dir).parent / path
    checkpoint_path = Path(local_config.get("checkpoint_path", path / "checkpoint.json"))
    if not checkpoint_path.is_absolute():
        checkpoint_path = Path(config.policy_dir).parent / checkpoint_path
    return EvidenceChain(LocalFileEvidenceBackend(path), signer, checkpoint_path=checkpoint_path)


def _build_evidence_anchor(
    config: AppConfig, alert_store: JsonlAlertStore | None = None
) -> HTTPAnchorBackend | None:
    evidence = config.evidence_config.get("evidence", {})
    anchor = evidence.get("anchor", {})
    if not anchor.get("enabled", False):
        return None
    auth = anchor["auth"]
    tls = anchor["tls"]
    receipt = anchor["receipt"]
    startup = anchor["startup"]
    root = Path(config.policy_dir).parent

    def resolve_path(value: str | None) -> str | None:
        if value is None:
            return None
        path = Path(value)
        return str(path if path.is_absolute() else root / path)

    return HTTPAnchorBackend(
        HTTPAnchorConfig(
            stream_id=anchor["stream_id"],
            base_url=anchor["base_url"],
            connect_timeout_seconds=float(anchor["connect_timeout_seconds"]),
            request_timeout_seconds=float(anchor["request_timeout_seconds"]),
            token=os.environ[auth["token_env"]],
            verify=tls["verify"],
            ca_file=resolve_path(tls.get("ca_file")),
            client_cert_file=resolve_path(tls.get("client_cert_file")),
            client_key_file=resolve_path(tls.get("client_key_file")),
            service_key_id=receipt["service_key_id"],
            public_key=ConfigLoader.resolve_anchor_public_key(config),
            unavailable_policy=startup["unavailable_policy"],
            conflict_policy=startup["conflict_policy"],
        ),
        alert_store=alert_store,
    )


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
    if backend_type == "encrypted_file":
        key_env = backend_config.get("key_env", "LC_SECRET_ENCRYPTION_KEY")
        return EncryptedFileSecretBackend(base_path, key_env=key_env)
    return FileSecretBackend(base_path)


# ---------------------------------------------------------------------------
# Runtime 工厂
# ---------------------------------------------------------------------------


def _build_go_kernel_bridge(config: AppConfig) -> GoKernelBridge | None:
    """根据 config.go_kernel_config 构造 Go 内核桥接；未启用时返回 None。"""
    gk = config.go_kernel_config.get("go_kernel", {})
    if not gk.get("enabled", False):
        return None
    base_url = gk.get("base_url", "http://127.0.0.1:8080")
    timeout = float(gk.get("timeout", 5.0))
    return GoKernelBridge(base_url=base_url, timeout=timeout)


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

    # v0.34.0：根据路径扩展名自动选择 SQLite 或 JSONL 状态后端
    _state_db_cache: dict[str, StateDatabase] = {}

    def _state_db_for(path: str) -> StateDatabase:
        normalized = str(Path(path).resolve())
        if normalized not in _state_db_cache:
            _state_db_cache[normalized] = StateDatabase(path)
        return _state_db_cache[normalized]

    def _is_sqlite_path(path: str) -> bool:
        return path.lower().endswith((".db", ".sqlite", ".sqlite3"))

    decision_store: DecisionStore
    if _is_sqlite_path(config.decision_log_path):
        decision_store = SqliteDecisionStore(_state_db_for(config.decision_log_path))
    else:
        decision_store = JsonlDecisionStore(config.decision_log_path)

    risk_state_store: RiskStateStore
    if _is_sqlite_path(config.risk_state_path):
        if config.risk_state_path == config.decision_log_path:
            risk_state_store = SqliteRiskStateStore(_state_db_for(config.decision_log_path))
        else:
            risk_state_store = SqliteRiskStateStore(_state_db_for(config.risk_state_path))
    else:
        risk_state_store = JsonlRiskStateStore(config.risk_state_path)

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
    http_executor = HTTPExecutor(http_client, config.http_tool_specs, secret_broker=secret_broker)
    local_executor = LocalFunctionExecutor(config.local_function_specs)
    alert_store = JsonlAlertStore(config.alert_store_path)
    harness_executor = HarnessExecutor(
        config.harness_tool_specs,
        config.harness_backends,
        execution_policy=config.harness_execution_policy,
        alert_store=alert_store,
    )
    executor_registry = ExecutorRegistry()
    for canonical_name in config.tool_mapping:
        executor_registry.register(canonical_name, mcp_executor)
    for canonical_name in config.http_tool_specs:
        executor_registry.register(canonical_name, http_executor)
    for canonical_name in config.local_function_specs:
        executor_registry.register(canonical_name, local_executor)
    for canonical_name in config.harness_tool_specs:
        executor_registry.register(canonical_name, harness_executor)

    from loop_controller.execution_mode import ExecutionModeResolver

    executor_registry.set_mode_resolver(
        ExecutionModeResolver(config.harness_execution_policy, harness_executor)
    )
    masker = Masker(config.masking_rules)
    budget_ledger = JsonlBudgetLedger(config.budget_ledger_path, alert_store=alert_store)
    session_manager = SessionManager(backend=JsonlSessionBackend(config.session_path))
    risk_manager = RiskStateManager(risk_state_store)
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
    revocation_path = Path(config.policy_dir).parent / "config" / "revocation.yaml"
    evidence_chain = _build_evidence_chain(config)
    evidence_paths: list[PersistenceTarget] = []
    if evidence_chain is not None:
        backend_path = getattr(evidence_chain._backend, "_base", None)
        if backend_path is not None:
            evidence_paths.append(PersistenceTarget("evidence", Path(backend_path) / "default.jsonl"))
        evidence_checkpoint = evidence_chain.checkpoint_path()
        if evidence_checkpoint is not None:
            evidence_paths.append(
                PersistenceTarget("evidence_checkpoint", evidence_checkpoint, replace=True)
            )
    persistence_status = PersistenceProbe(
        [
            PersistenceTarget("audit", Path(config.audit_log_path)),
            PersistenceTarget("decision", Path(config.decision_log_path)),
            PersistenceTarget("approval", Path(config.approval_store_path)),
            PersistenceTarget("budget", Path(config.budget_ledger_path)),
            PersistenceTarget("reservation", Path(config.reservation_store_path)),
            PersistenceTarget("authority", Path(config.authority_log_path)),
            PersistenceTarget("alert", Path(config.alert_store_path)),
            PersistenceTarget("task", Path(config.task_store_path)),
            PersistenceTarget("session", Path(config.session_path), critical=False),
            PersistenceTarget("conversation", Path(config.conversation_path), critical=False),
            PersistenceTarget("risk_state", Path(config.risk_state_path)),
            PersistenceTarget("revocation", revocation_path, replace=True),
            *evidence_paths,
        ],
        fsync_enabled=config.persistence.fsync_enabled,
        lock_timeout_seconds=config.persistence.lock_timeout_seconds,
        repair_incomplete_tail=config.persistence.repair_incomplete_tail,
        enforce_permissions=config.persistence.enforce_permissions,
        fail_on_unsafe_permissions=config.persistence.fail_on_unsafe_permissions,
    ).run()
    revocation_list = RevocationList.from_file(revocation_path)
    checkpoint = Checkpoint(
        profiles=config.profiles,
        policy_engine=policy_engine,
        policy_store=policy_store,
        gateway=gateway,
        executor_registry=executor_registry,
        identity=identity,
        session_manager=session_manager,
        risk_manager=risk_manager,
        decision_store=decision_store,
        budget_ledger=budget_ledger,
        reservation_store=reservation_store,
        permission_analyzer=CompositePermissionInteractionAnalyzer(
            ConfigPermissionInteractionAnalyzer(config.permission_rules),
            CapabilityBasedPermissionAnalyzer(config.capability_rules),
        ),
        authority_manager=authority_manager,
        revocation_list=revocation_list,
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
            **{
                name: BudgetCost(token_count=spec.cost_per_call)
                for name, spec in config.harness_tool_specs.items()
            },
        },
        masker=masker,
    )
    audit_key: bytes | None = None
    if config.audit_hash_algo == "hmac-sha256":
        audit_key = ConfigLoader.resolve_audit_key(config)
    evidence_anchor = _build_evidence_anchor(config, alert_store)
    anchor_receipt_verifier = (
        AnchorReceiptVerifier(
            {evidence_anchor.config.service_key_id: evidence_anchor.config.public_key}
        )
        if evidence_anchor is not None and evidence_anchor.config is not None
        else None
    )
    audit_index_path = Path(config.audit_log_path).with_suffix(".index.db")
    audit_store = JsonlAuditStore(
        config.audit_log_path,
        hash_algo=config.audit_hash_algo,
        hmac_key=audit_key,
        key_id=config.audit_key_id,
        evidence_chain=evidence_chain,
        alert_store=alert_store,
        anchor_backend=evidence_anchor,
        anchor_stream_id=config.evidence_config.get("evidence", {}).get("anchor", {}).get("stream_id"),
        anchor_receipt_verifier=anchor_receipt_verifier,
        index_path=audit_index_path,
    )
    checkpoint._audit_store = audit_store
    approval_manager = AsyncApprovalManager(
        JsonlApprovalStore(config.approval_store_path, alert_store=alert_store)
    )
    audit_analyzer = RuleBasedAuditAnalyzer(
        rules=config.audit_rules,
        audit_store=audit_store,
        alert_store=alert_store,
    )

    hot_reload_config = config.secrets_config.get("hot_reload", {})
    config_dir = Path(config.policy_dir).parent / "config"
    # 与 Runtime 共享同一可变集合，确保 HTTP/Harness 工具热更新后 controller 可见。
    http_tool_names = set(config.http_tool_specs)
    harness_tool_names = set(config.harness_tool_specs)
    hot_reloader = HotReloader(
        config_dir=config_dir,
        config_loader=ConfigLoader(),
        http_executor=http_executor,
        secret_broker=secret_broker,
        http_tool_names=http_tool_names,
        revocation_list=revocation_list,
        harness_executor=harness_executor,
        harness_tool_names=harness_tool_names,
        enabled=hot_reload_config.get("enabled", True),
        poll_interval_seconds=hot_reload_config.get("poll_interval_seconds", 30),
    )

    go_kernel_bridge = _build_go_kernel_bridge(config)

    return Runtime(
        classifier=RuleBasedClassifier(
            {name: spec.default_risk for name, spec in config.harness_tool_specs.items()}
        ),
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
        harness_executor=harness_executor,
        # 复用 HotReloader 持有的可变撤销列表，确保热更新对 Runtime 可见。
        revocation_list=revocation_list,
        evidence_anchor=evidence_anchor,
        persistence_status=persistence_status,
        go_kernel_bridge=go_kernel_bridge,
        local_agent_config=dict(
            config.go_kernel_config.get("go_kernel", {}).get("local_agent", {})
        ),
    )
