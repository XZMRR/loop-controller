import os
from typing import Any


def add(a: int, b: int) -> int:
    return a + b


def echo(**kwargs: Any) -> dict[str, Any]:
    return kwargs


def read_with_os_open(path: str) -> str:
    """使用 os.open 读取文件，用于测试沙箱是否封堵 builtins.open 绕过。"""
    fd = os.open(path, os.O_RDONLY)
    try:
        data = os.read(fd, 65536)
        return data.decode("utf-8", errors="replace")
    finally:
        os.close(fd)
