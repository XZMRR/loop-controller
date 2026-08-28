"""Prometheus metrics 封装（v0.18.0）。

本模块属于 server 扩展，依赖 ``prometheus-client``。
"""

from __future__ import annotations

from contextvars import ContextVar

try:
    from prometheus_client import (
        Counter,
        Gauge,
        Histogram,
        generate_latest,
    )
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "使用 loop_controller.metrics 需要先安装 prometheus-client: "
        "uv pip install 'loop-controller[server]'"
    ) from exc


REQUESTS_TOTAL = Counter(
    "loop_controller_requests_total",
    "Total HTTP requests",
    ["endpoint", "status"],
)

REQUEST_DURATION = Histogram(
    "loop_controller_request_duration_seconds",
    "HTTP request duration",
    ["endpoint"],
)

TOOL_CALLS_TOTAL = Counter(
    "loop_controller_tool_calls_total",
    "Total governed tool calls",
    ["tool_name", "status"],
)

APPROVAL_PENDING = Gauge(
    "loop_controller_approval_pending_total",
    "Current pending approval requests",
)

HARNESS_CALLS_TOTAL = Counter(
    "loop_controller_harness_calls_total",
    "Total Harness calls",
    ["backend", "tool", "status", "error_code"],
)

HARNESS_CALL_DURATION = Histogram(
    "loop_controller_harness_call_duration_seconds",
    "Harness call duration",
    ["backend", "tool"],
)

HARNESS_QUEUE_WAIT = Histogram(
    "loop_controller_harness_queue_wait_seconds",
    "Time spent waiting for a Harness backend concurrency slot",
    ["backend"],
)

HARNESS_IN_FLIGHT = Gauge(
    "loop_controller_harness_in_flight",
    "Current in-flight Harness calls",
    ["backend"],
)

HARNESS_OVERLOADED_TOTAL = Counter(
    "loop_controller_harness_overloaded_total",
    "Total Harness calls rejected by concurrency limits",
    ["backend"],
)

HARNESS_HEALTH = Gauge(
    "loop_controller_harness_health",
    "Harness backend health (1 for healthy, 0 otherwise)",
    ["backend"],
)


trace_id_var: ContextVar[str | None] = ContextVar("trace_id", default=None)


def set_trace_id(trace_id: str | None) -> None:
    """设置当前请求的 trace_id。"""
    trace_id_var.set(trace_id)


def get_trace_id() -> str | None:
    """获取当前请求的 trace_id。"""
    return trace_id_var.get()


def observe_request(endpoint: str, status_code: int, duration: float) -> None:
    """记录一次 HTTP 请求。"""
    status_label = _status_label(status_code)
    REQUESTS_TOTAL.labels(endpoint=endpoint, status=status_label).inc()
    REQUEST_DURATION.labels(endpoint=endpoint).observe(duration)


def observe_tool_call(tool_name: str, status: str) -> None:
    """记录一次工具调用治理结果。"""
    TOOL_CALLS_TOTAL.labels(tool_name=tool_name, status=status).inc()


def set_pending_approvals(count: int) -> None:
    """设置当前待审批请求数。"""
    APPROVAL_PENDING.set(count)


def observe_harness_call(
    backend: str,
    tool: str,
    status: str,
    error_code: str | None,
    duration: float,
) -> None:
    """记录一次 Harness 调用结果和执行耗时。"""
    HARNESS_CALLS_TOTAL.labels(
        backend=backend,
        tool=tool,
        status=status,
        error_code=error_code or "none",
    ).inc()
    HARNESS_CALL_DURATION.labels(backend=backend, tool=tool).observe(duration)


def observe_harness_queue_wait(backend: str, duration: float) -> None:
    """记录等待 Harness 并发槽位的耗时。"""
    HARNESS_QUEUE_WAIT.labels(backend=backend).observe(duration)


def set_harness_in_flight(backend: str, count: int) -> None:
    """设置 Harness 后端当前调用数。"""
    HARNESS_IN_FLIGHT.labels(backend=backend).set(count)


def observe_harness_overloaded(backend: str) -> None:
    """记录一次 Harness 过载拒绝。"""
    HARNESS_OVERLOADED_TOTAL.labels(backend=backend).inc()


def set_harness_health(backend: str, healthy: bool) -> None:
    """设置 Harness 后端健康值。"""
    HARNESS_HEALTH.labels(backend=backend).set(1 if healthy else 0)


def render_metrics() -> bytes:
    """返回 Prometheus 指标文本。"""
    return generate_latest()


def _status_label(status_code: int) -> str:
    if status_code < 300:
        return "2xx"
    if status_code < 400:
        return "3xx"
    if status_code < 500:
        return "4xx"
    return "5xx"
