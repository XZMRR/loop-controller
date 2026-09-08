"""集成测试用的本地函数工具。

这些函数由 LocalFunctionExecutor 在独立子进程中调用，
因此参数和返回值必须可 JSON 序列化。
"""

from __future__ import annotations

import time


def add(a: int, b: int) -> int:
    """返回两数之和。"""
    return a + b


def echo(text: str) -> str:
    """回显输入文本。"""
    return text


def send_email(to: str, subject: str, body: str) -> dict[str, str]:
    """模拟发送邮件，返回发送结果。"""
    return {"status": "sent", "to": to, "subject": subject}


def raise_error(message: str) -> str:
    """故意抛出异常，用于测试执行失败时的错误透传。"""
    raise RuntimeError(message)


def hang_forever(seconds: float) -> str:
    """睡眠超过配置的超时时间，用于测试 harness_timeout。"""
    time.sleep(seconds)
    return "should not reach here"
