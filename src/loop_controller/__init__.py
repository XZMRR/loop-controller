"""Loop Controller: Agent 组织的制度基础设施.

为 AI Agent 提供 R0-R3 四层治理模型的运行时实现。本包按《MVP 完备方案：纯工具调用版 v1.1》
渐进式构建；`models.py` 是唯一权威 Schema 来源。
"""

from loop_controller.approval_manager import AsyncApprovalManager
from loop_controller.checkpoint import Checkpoint, CheckpointError
from loop_controller.classifier import LightweightClassifier, RuleBasedClassifier
from loop_controller.mcp_gateway import MCPGateway, MCPGatewayError
from loop_controller.models import (
    ActionProposal,
    Agent,
    ApprovalRecord,
    ApprovalRequest,
    AuditEvent,
    BudgetCost,
    CapabilityProfile,
    Decision,
    PlannedAction,
    RiskLevel,
    RiskProfile,
    RiskSignal,
    Task,
    Tool,
    ToolPermission,
    ToolResult,
    Verdict,
)
from loop_controller.planner import Planner, ScriptedPlanner

__all__ = [
    "ActionProposal",
    "Agent",
    "ApprovalRecord",
    "ApprovalRequest",
    "AsyncApprovalManager",
    "AuditEvent",
    "BudgetCost",
    "CapabilityProfile",
    "Checkpoint",
    "CheckpointError",
    "Decision",
    "LightweightClassifier",
    "MCPGateway",
    "MCPGatewayError",
    "PlannedAction",
    "Planner",
    "RiskLevel",
    "RiskProfile",
    "RiskSignal",
    "RuleBasedClassifier",
    "ScriptedPlanner",
    "Task",
    "Tool",
    "ToolPermission",
    "ToolResult",
    "Verdict",
]
