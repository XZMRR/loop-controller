"""Loop Controller: Agent 组织的制度基础设施.

为 AI Agent 提供 R0-R3 四层治理模型的运行时实现，包括任务上下文、
Agent 执行、能力画像、动作申报、风险分类、策略判定、审计等核心模块。
"""

from loop_controller.action_proposal import ActionProposal
from loop_controller.agent import Agent
from loop_controller.audit import AuditEvent, AuditLogger, JsonlAuditLogger
from loop_controller.budget import BudgetCost, BudgetLedger, InMemoryBudgetLedger
from loop_controller.capability_profile import CapabilityProfile, ToolPermission
from loop_controller.checkpoint import Checkpoint, CheckpointConfig
from loop_controller.classifier import LightweightClassifier, RiskSignal, RuleBasedClassifier
from loop_controller.decision import Decision
from loop_controller.mcp_gateway import MCPGateway, MockMCPGateway
from loop_controller.permission_interaction import (
    PermissionInteractionAnalyzer,
    StaticPermissionInteractionAnalyzer,
)
from loop_controller.policy_engine import (
    MockPolicyEngine,
    OPAPolicyEngine,
    PolicyEngine,
    PolicyEngineError,
)
from loop_controller.r0_delegate import ApprovalRecord, ApprovalRequest, ConfigR0Delegate, R0Delegate
from loop_controller.risk_state import InMemoryRiskStateManager, RiskProfile, RiskStateManager
from loop_controller.task import Task
from loop_controller.tool import Tool, ToolResult

__all__ = [
    "ActionProposal",
    "Agent",
    "ApprovalRecord",
    "ApprovalRequest",
    "AuditEvent",
    "AuditLogger",
    "BudgetCost",
    "BudgetLedger",
    "CapabilityProfile",
    "Checkpoint",
    "CheckpointConfig",
    "ConfigR0Delegate",
    "Decision",
    "InMemoryBudgetLedger",
    "InMemoryRiskStateManager",
    "JsonlAuditLogger",
    "LightweightClassifier",
    "MCPGateway",
    "MockMCPGateway",
    "MockPolicyEngine",
    "OPAPolicyEngine",
    "PermissionInteractionAnalyzer",
    "PolicyEngine",
    "PolicyEngineError",
    "RiskProfile",
    "RiskSignal",
    "RiskStateManager",
    "RuleBasedClassifier",
    "StaticPermissionInteractionAnalyzer",
    "Task",
    "Tool",
    "ToolPermission",
    "ToolResult",
    "R0Delegate",
]
