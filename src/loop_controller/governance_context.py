"""动态治理上下文构造（v0.3.0 Iteration 4）。

``build_governance_context`` 是 R2 使用的 ``task_context`` 的唯一权威来源。
它由 Runtime/Checkpoint 从 Task 与 ConversationContext 确定性构建，
不允许 Agent 自报内容覆盖。
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from loop_controller.models import ConversationContext, ConversationMessage, Task

# R2 input 默认总长度限制
_DEFAULT_R2_LIMIT = 2000
# 纳入最近消息数
_MAX_USER_MESSAGES = 5
_MAX_AGENT_MESSAGES = 3


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_messages(messages: list[ConversationMessage], prefix: str) -> list[str]:
    lines: list[str] = []
    for msg in messages:
        role = "用户补充" if msg.role == "user" else "Agent 回复"
        lines.append(f"{role}：{msg.content}")
    return [f"{prefix}\n" + "\n".join(lines)] if lines else []


def build_governance_context(
    task: Task,
    conversation_context: ConversationContext,
    *,
    r2_limit: int = _DEFAULT_R2_LIMIT,
    max_user_messages: int = _MAX_USER_MESSAGES,
    max_agent_messages: int = _MAX_AGENT_MESSAGES,
) -> str:
    """构造 R2 使用的治理上下文。

    规则：
    1. 当前 Task.description 永远置于最前；
    2. 纳入最近 ``max_user_messages`` 条用户消息；
    3. 纳入最近 ``max_agent_messages`` 条 Agent 消息；
    4. 同一 session 内其他 Task 的消息在排序上靠后（v0.3.0 先不混入，
       避免过度复杂；仅当前 Task 的消息参与 R2 上下文）；
    5. 超长时从尾部截断并标注 ``[truncated, total=N chars]``。

    注意：此函数只输出字符串，不生成 hash/hash 由 policy_engine 写入
    ``context_meta``。
    """
    user_messages = [
        m for m in conversation_context.messages if m.role == "user" and m.task_id == task.task_id
    ][-max_user_messages:]
    agent_messages = [
        m for m in conversation_context.messages if m.role == "agent" and m.task_id == task.task_id
    ][-max_agent_messages:]

    parts: list[str] = [f"当前任务：{task.description}"]
    parts.extend(_format_messages(user_messages, "用户补充："))
    parts.extend(_format_messages(agent_messages, "Agent 最近回复："))

    text = "\n".join(parts)
    if len(text) <= r2_limit:
        return text
    return f"{text[:r2_limit]}[truncated, total={len(text)} chars]"


def build_context_meta(
    task: Task,
    conversation_context: ConversationContext,
    governance_context: str,
) -> dict[str, object]:
    """生成用于 Rego input 与审计的上下文元数据。"""
    task_messages = [m for m in conversation_context.messages if m.task_id == task.task_id]
    return {
        "session_id": conversation_context.session_id,
        "message_count": len(task_messages),
        "context_length": len(governance_context),
        "context_hash": hashlib.sha256(governance_context.encode("utf-8")).hexdigest(),
        "built_at": _utc_now().isoformat(),
    }
