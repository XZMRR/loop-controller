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

ANCHOR_PUBLISH_TOTAL = Counter(
    "loop_controller_anchor_publish_total",
    "Total trusted anchor publish attempts",
    ["status", "error_code"],
)

ANCHOR_PUBLISH_DURATION = Histogram(
    "loop_controller_anchor_publish_duration_seconds",
    "Trusted anchor publish duration",
)

ANCHOR_LAST_SUCCESS_SEQ = Gauge(
    "loop_controller_anchor_last_success_seq",
    "Last successfully published trusted anchor sequence",
)

ANCHOR_LAG_EVENTS = Gauge(
    "loop_controller_anchor_lag_events",
    "Current local events not covered by the trusted anchor",
)

ANCHOR_STATUS = Gauge(
    "loop_controller_anchor_status",
    "Trusted anchor health (1 for healthy, 0 otherwise)",
)

ANCHOR_CONFLICTS_TOTAL = Counter(
    "loop_controller_anchor_conflicts_total",
    "Total trusted anchor conflicts",
)

PERSISTENCE_FSYNC_ENABLED = Gauge(
    "loop_controller_persistence_fsync_enabled",
    "Whether durable fsync is enabled (1) or disabled (0)",
)

PERSISTENCE_DURABILITY_SAFE = Gauge(
    "loop_controller_persistence_durability_safe",
    "Whether persistence durability is considered safe (1) or unsafe (0)",
)

PERSISTENCE_TAIL_REPAIRS_TOTAL = Counter(
    "loop_controller_persistence_tail_repairs_total",
    "Total durable JSONL tail repairs",
)

PERSISTENCE_CORRUPTED_TOTAL = Counter(
    "loop_controller_persistence_corrupted_total",
    "Total durable store corruption events",
    ["store"],
)

PERSISTENCE_LOCK_FAILURES_TOTAL = Counter(
    "loop_controller_persistence_lock_failures_total",
    "Total durable I/O lock acquisition failures",
)

PERSISTENCE_FSYNC_DURATION = Histogram(
    "loop_controller_persistence_fsync_duration_seconds",
    "Durable fsync call duration",
)

PERSISTENCE_LOCK_WAIT_DURATION = Histogram(
    "loop_controller_persistence_lock_wait_seconds",
    "Durable I/O lock acquisition wait duration",
)


_ANCHOR_ERROR_CODES = {
    "none",
    "anchor_authentication_failed",
    "anchor_conflict",
    "anchor_http_error",
    "anchor_rate_limited",
    "anchor_receipt_invalid",
    "anchor_rollback_rejected",
    "anchor_timeout",
    "anchor_unavailable",
}


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


def observe_anchor_publish(
    status: str,
    error_code: str | None,
    duration: float,
) -> None:
    """记录一次 Anchor 发布；未知错误统一归入 other，避免 label 基数失控。"""
    normalized_code = error_code or "none"
    if normalized_code not in _ANCHOR_ERROR_CODES:
        normalized_code = "other"
    normalized_status = status if status in {"success", "error"} else "error"
    ANCHOR_PUBLISH_TOTAL.labels(status=normalized_status, error_code=normalized_code).inc()
    ANCHOR_PUBLISH_DURATION.observe(duration)


def set_anchor_state(status: str, last_success_seq: int, lag_events: int) -> None:
    """更新不带高基数 label 的 Anchor 状态指标。"""
    ANCHOR_LAST_SUCCESS_SEQ.set(max(0, last_success_seq))
    ANCHOR_LAG_EVENTS.set(max(0, lag_events))
    ANCHOR_STATUS.set(1 if status == "healthy" else 0)


def observe_anchor_conflict() -> None:
    """记录一次确定性 Anchor 冲突。"""
    ANCHOR_CONFLICTS_TOTAL.inc()


def render_metrics() -> bytes:
    """返回 Prometheus 指标文本。"""
    return generate_latest()


def set_persistence_durability(safe: bool, fsync_enabled: bool) -> None:
    """更新持久化 durability 与 fsync 状态指标。"""
    PERSISTENCE_DURABILITY_SAFE.set(1 if safe else 0)
    PERSISTENCE_FSYNC_ENABLED.set(1 if fsync_enabled else 0)


def observe_persistence_fsync(duration: float) -> None:
    """记录一次 fsync 耗时。"""
    PERSISTENCE_FSYNC_DURATION.observe(max(0.0, duration))


def observe_persistence_lock_wait(duration: float) -> None:
    """记录一次 durable I/O 锁等待耗时。"""
    PERSISTENCE_LOCK_WAIT_DURATION.observe(max(0.0, duration))


def observe_persistence_tail_repair() -> None:
    """记录一次尾部修复。"""
    PERSISTENCE_TAIL_REPAIRS_TOTAL.inc()


def observe_persistence_corruption(store: str) -> None:
    """记录一次持久化存储损坏事件。"""
    PERSISTENCE_CORRUPTED_TOTAL.labels(store=store).inc()


def observe_persistence_lock_failure() -> None:
    """记录一次锁获取失败。"""
    PERSISTENCE_LOCK_FAILURES_TOTAL.inc()


def _status_label(status_code: int) -> str:
    if status_code < 300:
        return "2xx"
    if status_code < 400:
        return "3xx"
    if status_code < 500:
        return "4xx"
    return "5xx"
