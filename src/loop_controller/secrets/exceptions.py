"""Secret Broker 异常（v0.22.0）。"""

from __future__ import annotations


class SecretError(Exception):
    """Secret Broker 通用错误。"""


class SecretNotFoundError(SecretError):
    """Secret 不存在、已过期、无权访问或 key 无法提取。"""

    def __init__(self, message: str, *, ref_name: str | None = None) -> None:
        super().__init__(message)
        self.ref_name = ref_name
