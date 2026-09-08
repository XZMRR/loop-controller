"""本地函数子进程执行入口（v0.23.0）。

通过 stdin 读取 JSON 任务，在受限子进程中导入并执行目标函数，
将结果或错误通过 stdout 以 JSON 返回。
"""

from __future__ import annotations

import builtins
import importlib
import inspect
import io
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any


def _path_allowed(path: Any, allowed_patterns: list[str]) -> bool:
    """判断路径是否命中任一允许 glob 模式。"""
    from loop_controller.utils.globmatch import glob_match

    try:
        path_str = str(Path(path).resolve())
    except Exception:
        path_str = str(path)
    return any(glob_match(pattern, path_str) for pattern in allowed_patterns)


def _make_restricted_open(
    allowed_patterns: list[str],
    original_open: Callable[..., Any],
) -> Callable[..., Any]:
    """包装 builtins.open，限制可访问路径。"""

    def restricted_open(*args: Any, **kwargs: Any) -> Any:
        path = args[0] if args else kwargs.get("file")
        if path is not None and not _path_allowed(path, allowed_patterns):
            raise PermissionError(
                f"local_function_sandbox_violation: path {path} not in allowed_paths"
            )
        return original_open(*args, **kwargs)

    return restricted_open


def _make_restricted_os_open(
    allowed_patterns: list[str],
    original_os_open: Callable[..., int],
) -> Callable[..., int]:
    """包装 os.open，限制可访问路径（封堵 builtins.open 绕过）。"""

    def restricted_os_open(path: Any, *args: Any, **kwargs: Any) -> int:
        if isinstance(path, (str, os.PathLike)) and not _path_allowed(path, allowed_patterns):
            raise PermissionError(
                f"local_function_sandbox_violation: path {path} not in allowed_paths"
            )
        return original_os_open(path, *args, **kwargs)

    return restricted_os_open


def _make_restricted_path_open(
    allowed_patterns: list[str],
    original_path_open: Callable[..., Any],
) -> Callable[..., Any]:
    """包装 pathlib.Path.open，限制可访问路径（封堵 io.open 绕过）。"""

    def restricted_path_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        if not _path_allowed(self, allowed_patterns):
            raise PermissionError(
                f"local_function_sandbox_violation: path {self} not in allowed_paths"
            )
        return original_path_open(self, *args, **kwargs)

    return restricted_path_open


def _run_task(payload: dict[str, Any]) -> dict[str, Any]:
    module_name = payload["module"]
    function_name = payload["function"]
    arguments = payload.get("arguments", {})
    sandbox = payload.get("sandbox", {})
    allowed_paths = sandbox.get("allowed_paths", [])

    try:
        module = importlib.import_module(module_name)
    except Exception as exc:
        return {
            "status": "error",
            "error_code": "local_function_import_error",
            "content": f"无法导入模块 {module_name}: {exc}",
        }

    try:
        func = getattr(module, function_name)
    except AttributeError as exc:
        return {
            "status": "error",
            "error_code": "local_function_import_error",
            "content": f"模块 {module_name} 缺少函数 {function_name}: {exc}",
        }

    original_open = builtins.open
    original_os_open = os.open
    original_io_open = io.open
    original_path_open = Path.open
    if allowed_paths:
        restricted_open = _make_restricted_open(allowed_paths, original_open)
        builtins.open = restricted_open
        io.open = restricted_open
        os.open = _make_restricted_os_open(allowed_paths, original_os_open)
        # 通过 setattr 绕开 mypy 对 classmethod 直接赋值的检查
        setattr(Path, "open", _make_restricted_path_open(allowed_paths, original_path_open))

    try:
        if inspect.iscoroutinefunction(func):
            import asyncio

            result = asyncio.run(func(**arguments))
        else:
            result = func(**arguments)
        return {"status": "success", "content": result}
    except PermissionError as exc:
        return {
            "status": "error",
            "error_code": "local_function_sandbox_violation",
            "content": str(exc),
        }
    except Exception as exc:
        return {
            "status": "error",
            "error_code": "local_function_runtime_error",
            "content": f"{type(exc).__name__}: {exc}",
        }
    finally:
        builtins.open = original_open
        os.open = original_os_open
        io.open = original_io_open
        setattr(Path, "open", original_path_open)


def main() -> None:
    """从 stdin 读取任务 JSON，执行后向 stdout 输出结果 JSON。"""
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        _write_result(
            {
                "status": "error",
                "error_code": "local_function_invalid_input",
                "content": f"输入 JSON 解析失败: {exc}",
            }
        )
        return

    result = _run_task(payload)
    _write_result(result)


def _write_result(result: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
