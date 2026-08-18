"""LLMPlanner（T3.5）：基于真实 LLM 的 R1 规划器。

非治理组件，只负责按 JSON Schema 输出动作草案；所有治理判定仍由 Checkpoint/R2 完成。
失败不重试、不纠错，直接写审计并返回 None 终止任务。
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from loop_controller.checkpoint import BudgetLedger
from loop_controller.infra.audit_store import AuditStore
from loop_controller.infra.config_loader import LLMPlannerConfig
from loop_controller.mcp_gateway import MCPGateway
from loop_controller.models import (
    Agent,
    AuditEvent,
    BudgetCost,
    CapabilityProfile,
    ConversationContext,
    ConversationMessage,
    PlannedAction,
    Task,
    Tool,
    ToolResult,
    UserQuestion,
)


# ---------------------------------------------------------------------------
# Prompt 常量
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """你是一个企业研究助手，通过调用工具完成用户任务。

规则：
1. 你的每次输出必须且仅为一个 JSON 对象，格式：
   {"action": "call_tool", "tool_name": "...", "arguments": {...}, "reason": "..."}
   或 {"action": "ask_user", "question": "..."}
   或 {"action": "finish"}
2. 你只能使用下方列出的工具。所有工具调用都会被独立的治理层审核，
   可能被修改、拒绝或要求人工审批——这是正常流程。如果被拦截，
   阅读拦截原因，选择合法替代方案继续完成任务。
3. 工具结果很大时，请在读到内容的下一步立即处理（摘要或写出），
   历史结果之后只保留摘要。
4. 当信息不足、需要用户澄清时，输出 {"action": "ask_user", "question": "..."}。
5. 任务完成或无法继续时，输出 {"action": "finish"}。
"""

_ASK_SECTION = "请输出下一个动作的 JSON："

# 历史摘要参数
_MAX_RECENT_CONTENT = 2000
_MAX_SUMMARY_CONTENT = 80
_RAW_PREVIEW_LEN = 200


# ---------------------------------------------------------------------------
# LLMClient 协议与 httpx 实现
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LLMResponse:
    """LLM 返回的原始内容 + usage（prompt_tokens / completion_tokens 可能为空）。"""

    content: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


@runtime_checkable
class LLMClient(Protocol):
    """OpenAI 兼容 chat/completions 客户端抽象，便于单测注入 fake。"""

    async def chat(
        self,
        messages: list[dict[str, Any]],
        config: LLMPlannerConfig,
    ) -> LLMResponse: ...


class HttpxLLMClient:
    """使用 httpx 调用 OpenAI 兼容 ``chat/completions``（不依赖 openai 库）。"""

    async def chat(
        self,
        messages: list[dict[str, Any]],
        config: LLMPlannerConfig,
    ) -> LLMResponse:
        # 密钥纪律：只从环境变量读取，绝不使用硬编码或写入日志
        api_key = os.environ.get(config.api_key_env)
        if not api_key:
            raise RuntimeError(f"环境变量 {config.api_key_env} 未设置")

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": config.model,
            "messages": messages,
            "max_tokens": config.max_tokens,
            "temperature": config.temperature,
        }

        async with httpx.AsyncClient(trust_env=False, timeout=config.timeout_s) as client:
            resp = await client.post(
                f"{config.base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()

        choice = data.get("choices", [{}])[0]
        message = choice.get("message", {})
        usage = data.get("usage") or {}
        return LLMResponse(
            content=message.get("content", ""),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
        )


# ---------------------------------------------------------------------------
# JSON Schema 校验（内部 Pydantic 模型）
# ---------------------------------------------------------------------------


class _LLMOutput(BaseModel):
    """LLM 输出必须满足的 Schema；action=finish 时其余字段可为空。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: str
    tool_name: str | None = None
    arguments: dict[str, Any] | None = None
    reason: str | None = None
    question: str | None = None


# ---------------------------------------------------------------------------
# 提示词与历史摘要
# ---------------------------------------------------------------------------


def _truncate(text: str, limit: int) -> str:
    """截断文本并在尾部标注原始长度。"""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}...[truncated, total={len(text)} chars]"


def _content_to_str(content: Any) -> str:
    """ToolResult.content 可能是任意类型，统一为字符串。"""
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        return str(content)


def _format_tools(tools: list[Tool]) -> str:
    """把授权工具列表序列化为 JSON 段落。"""
    lines: list[str] = []
    for tool in tools:
        entry = {
            "name": tool.canonical_name,
            "description": tool.description,
            "input_schema": tool.input_schema,
        }
        lines.append(json.dumps(entry, ensure_ascii=False))
    if not lines:
        return "（当前 Profile 未授权任何工具）"
    return "\n".join(lines)


def _format_history(observations: list[ToolResult]) -> str:
    """分层摘要：最近 1 步完整，更早步骤一行摘要。"""
    if not observations:
        return "（无）"

    lines: list[str] = []
    for i, obs in enumerate(observations):
        is_recent = i == len(observations) - 1
        if is_recent:
            content = _truncate(_content_to_str(obs.content), _MAX_RECENT_CONTENT)
            lines.append(
                f"[最近] tool={obs.tool_name}, status={obs.status}\n"
                f"content: {content}"
            )
        else:
            lines.append(f"[{i}] {obs.tool_name} → {_one_line_summary(obs)}")
    return "\n".join(lines)


def _one_line_summary(obs: ToolResult) -> str:
    """早期步骤的一行摘要（规则生成，不再调用 LLM）。"""
    if obs.status == "success":
        content = _content_to_str(obs.content)
        preview = _truncate(content, _MAX_SUMMARY_CONTENT).replace("\n", " ")
        return f"成功，返回 {preview}"
    if obs.status == "blocked":
        return f"被治理层拦截：{_content_to_str(obs.content)}"
    if obs.status == "error":
        error = obs.error_code or "unknown"
        return f"执行失败：{error}"
    return f"状态 {obs.status}"


def _format_conversation(context: ConversationContext) -> str:
    """把会话上下文格式化为 prompt 段落。"""
    if not context.messages:
        return "（无）"
    lines: list[str] = []
    for msg in context.messages:
        role = "用户" if msg.role == "user" else "Agent"
        lines.append(f"[{role}] {msg.content}")
    return "\n".join(lines)


def _build_prompt(
    task: Task,
    tools: list[Tool],
    observations: list[ToolResult],
    conversation_context: ConversationContext,
) -> str:
    """按五段结构组装 prompt。"""
    sections = [
        f"[task]\n{task.description}",
        f"[conversation]\n{_format_conversation(conversation_context)}",
        f"[tools]\n{_format_tools(tools)}",
        f"[history]\n{_format_history(observations)}",
        f"[ask]\n{_ASK_SECTION}",
    ]
    return "\n\n".join(sections)


def _messages(user_prompt: str) -> list[dict[str, Any]]:
    """system + user 两段式消息列表。"""
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]


# ---------------------------------------------------------------------------
# 响应解析
# ---------------------------------------------------------------------------


def _extract_first_json(text: str) -> str | None:
    """从 LLM 输出中提取第一个完整 JSON 对象，允许前后有 markdown 等噪声。"""
    # 先去掉 markdown 代码块标记
    stripped = re.sub(r"```(?:json)?\s*", "", text).strip()
    start = stripped.find("{")
    if start == -1:
        return None
    depth = 0
    for i, ch in enumerate(stripped[start:], start=start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return stripped[start : i + 1]
    return None


def _parse_response(
    content: str, allowed_tools: list[str]
) -> tuple[PlannedAction | UserQuestion | None, dict[str, Any] | None]:
    """解析 LLM 输出。

    返回 (PlannedAction, None) 或 (UserQuestion, None) 或 (None, None) 表示 finish；
    返回 (None, error_dict) 表示失败原因（含原始输出预览）。
    """
    raw_json = _extract_first_json(content)
    if raw_json is None:
        return None, {"reason": "未找到合法 JSON 对象", "raw_preview": content[:_RAW_PREVIEW_LEN]}

    try:
        obj = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        return None, {"reason": f"JSON 解析失败：{exc}", "raw_preview": content[:_RAW_PREVIEW_LEN]}

    try:
        parsed = _LLMOutput(**obj)
    except ValidationError as exc:
        return None, {"reason": f"Schema 校验失败：{exc}", "raw_preview": content[:_RAW_PREVIEW_LEN]}

    if parsed.action not in ("call_tool", "ask_user", "finish"):
        return None, {
            "reason": f"action 必须是 call_tool/ask_user/finish，得到 {parsed.action!r}",
            "raw_preview": content[:_RAW_PREVIEW_LEN],
        }

    if parsed.action == "finish":
        return None, None

    if parsed.action == "ask_user":
        if not parsed.question:
            return None, {
                "reason": "ask_user 时 question 必须存在",
                "raw_preview": content[:_RAW_PREVIEW_LEN],
            }
        return UserQuestion(question=parsed.question, reason=parsed.reason), None

    # call_tool 必须字段
    if not parsed.tool_name or parsed.arguments is None or not parsed.reason:
        return None, {
            "reason": "call_tool 时 tool_name/arguments/reason 必须存在",
            "raw_preview": content[:_RAW_PREVIEW_LEN],
        }

    # 工具白名单预检（提前失败优化，R2 仍保有最终权威）
    if parsed.tool_name not in allowed_tools:
        return None, {
            "reason": f"工具 {parsed.tool_name!r} 不在当前 Profile 授权列表中",
            "raw_preview": content[:_RAW_PREVIEW_LEN],
        }

    return PlannedAction(
        tool_name=parsed.tool_name,
        arguments=parsed.arguments,
        reason=parsed.reason,
    ), None


# ---------------------------------------------------------------------------
# Token 估算
# ---------------------------------------------------------------------------


def _estimate_prompt_tokens(prompt: str) -> int:
    """本地粗估：每 3 字符约 1 token（演示用，不精确）。"""
    return max(1, len(prompt) // 3)


def _actual_tokens(response: LLMResponse, prompt_text: str, config: LLMPlannerConfig) -> int:
    """优先使用 LLM 返回的 usage，本地模型无 usage 时回退到粗估。"""
    if response.prompt_tokens is not None and response.completion_tokens is not None:
        return response.prompt_tokens + response.completion_tokens
    return _estimate_prompt_tokens(prompt_text) + max(1, len(response.content) // 3)


# ---------------------------------------------------------------------------
# LLMPlanner
# ---------------------------------------------------------------------------


class LLMPlanner:
    """真实 LLM 规划器（T3.5）。"""

    def __init__(
        self,
        *,
        client: LLMClient,
        config: LLMPlannerConfig,
        gateway: MCPGateway,
        budget_ledger: BudgetLedger | None = None,
        audit_store: AuditStore | None = None,
        profiles: dict[str, CapabilityProfile],
    ) -> None:
        self._client = client
        self._config = config
        self._gateway = gateway
        self._budget_ledger = budget_ledger
        self._audit_store = audit_store
        self._profiles = profiles

    async def next_action(
        self,
        task: Task,
        agent: Agent,
        observations: list[ToolResult],
        conversation_context: ConversationContext,
    ) -> PlannedAction | UserQuestion | None:
        """调用 LLM 获取下一步动作；失败时写审计并返回 None。"""
        profile = self._profiles.get(agent.profile_id)
        if profile is None:
            self._audit_error(
                task,
                agent,
                {"reason": f"找不到 Profile {agent.profile_id!r}", "raw_preview": ""},
            )
            return None

        # 拉取授权工具列表（用于 prompt 与白名单预检）
        try:
            tools = await self._gateway.list_tools(profile)
        except Exception as exc:  # noqa: BLE001
            self._audit_error(
                task,
                agent,
                {"reason": f"获取工具列表失败：{exc}", "raw_preview": ""},
            )
            return None

        prompt = _build_prompt(task, tools, observations, conversation_context)
        estimated = _estimate_prompt_tokens(prompt) + self._config.max_tokens

        # 设置 per-task 预算上限（与 Checkpoint 共用同一个 ledger）
        if self._budget_ledger is not None and hasattr(self._budget_ledger, "set_budget"):
            self._budget_ledger.set_budget(task.task_id, profile.max_budget_token)

        # 路径 A：预占预估 token 上限
        if self._budget_ledger is not None:
            if not self._budget_ledger.check_and_reserve(
                task.task_id, BudgetCost(token_count=estimated)
            ):
                self._audit_budget_exceeded(task, agent, estimated, profile.max_budget_token)
                return None

        try:
            response = await self._client.chat(_messages(prompt), self._config)
        except Exception as exc:  # noqa: BLE001
            # LLM 调用失败：退还预占额度，记录审计后终止任务
            if self._budget_ledger is not None:
                self._budget_ledger.refund(task.task_id, BudgetCost(token_count=estimated))
            self._audit_error(
                task,
                agent,
                {"reason": f"LLM 调用失败：{exc}", "raw_preview": ""},
            )
            return None

        actual = _actual_tokens(response, prompt, self._config)

        # 按真实 usage 确认消耗；若实际值高于预估，先补占差额
        if self._budget_ledger is not None:
            if actual > estimated:
                extra = actual - estimated
                if not self._budget_ledger.check_and_reserve(
                    task.task_id, BudgetCost(token_count=extra)
                ):
                    # 即使补占失败，也已真实消耗 token：确认实际值并释放预占
                    self._budget_ledger.commit(task.task_id, BudgetCost(token_count=actual))
                    self._budget_ledger.refund(
                        task.task_id, BudgetCost(token_count=estimated)
                    )
                    self._audit_budget_exceeded(task, agent, actual, profile.max_budget_token)
                    return None
            self._budget_ledger.commit(task.task_id, BudgetCost(token_count=actual))
            if estimated > actual:
                self._budget_ledger.refund(
                    task.task_id, BudgetCost(token_count=estimated - actual)
                )

        allowed_tools = [t.canonical_name for t in tools]
        planned, err = _parse_response(response.content, allowed_tools)
        if err is not None:
            self._audit_error(task, agent, err)
            return None

        return planned

    # -- 审计辅助 -----------------------------------------------------------

    def _audit_error(
        self,
        task: Task,
        agent: Agent,
        error: dict[str, Any],
    ) -> None:
        """写 planner_error 审计事件；错误信息中绝不包含 API key。"""
        if self._audit_store is None:
            return
        self._audit_store.append(
            AuditEvent(
                event_id=uuid.uuid4().hex,
                trace_id=task.task_id,
                session_id=task.session_id,
                actor_type="system",
                actor_id=agent.agent_id,
                action="planner_error",
                reason=f"planner error: {error['reason']}",
                metadata={"planner_error": error},
            )
        )

    def _audit_budget_exceeded(
        self,
        task: Task,
        agent: Agent,
        estimated: int,
        max_budget_token: int,
    ) -> None:
        """写 budget exceeded 审计事件。"""
        if self._audit_store is None:
            return
        self._audit_store.append(
            AuditEvent(
                event_id=uuid.uuid4().hex,
                trace_id=task.task_id,
                session_id=task.session_id,
                actor_type="system",
                actor_id=agent.agent_id,
                action="planner_error",
                reason="planner budget exceeded",
                metadata={
                    "planner_budget_exceeded": True,
                    "estimated_tokens": estimated,
                    "max_budget_token": max_budget_token,
                },
            )
        )
