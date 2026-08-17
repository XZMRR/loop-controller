"""POSIX glob 匹配工具（全局唯一实现）.

与 ``allowed_args`` / ``denied_args`` / 权限组合规则 / Masker 共用同一套
POSIX glob 语义，避免各处实现漂移（开发指南踩坑 #6 同类问题）。
"""

from __future__ import annotations

import fnmatch
import re


def glob_match(pattern: str, value: str) -> bool:
    """判断 value 是否匹配 pattern（POSIX glob 语义）.

    基于 ``fnmatch``：``*`` 匹配任意字符（含跨目录），``?`` 单字符，
    ``[...]`` 字符集。大小写敏感。
    """
    return fnmatch.fnmatchcase(value, pattern)


def compile_glob(pattern: str) -> None:
    """校验 pattern 是合法 glob（非法结构抛 ValueError）.

    ``fnmatch.translate`` 会把非法方括号转义为字面量、永不抛错，因此必须
    自行解析 ``[...]`` 是否闭合，再用 ``re.compile`` 兜底。
    """
    _validate_brackets(pattern)
    re.compile(fnmatch.translate(pattern))


def _validate_brackets(pattern: str) -> None:
    """检查方括号表达式是否成对闭合（POSIX glob 语义）。"""
    i = 0
    n = len(pattern)
    while i < n:
        if pattern[i] == "[":
            j = i + 1
            if j < n and pattern[j] in ("!", "^"):
                j += 1
            if j < n and pattern[j] == "]":
                j += 1
            while j < n and pattern[j] != "]":
                j += 1
            if j >= n:
                raise ValueError("unclosed '[' in pattern")
            i = j
        i += 1
