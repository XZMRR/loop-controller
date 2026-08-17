"""规范 JSON 序列化（§7.3 / 开发指南踩坑 #6）。

为 ``args_hash``、哈希链 ``prev_hash``、超长字段截断的摘要计算提供**唯一**
canonical 表示：键排序、无空白、UTF-8 编码、``ensure_ascii=False``。
"""

from __future__ import annotations

import json
from typing import Any


def canonical_json(obj: Any) -> str:
    """返回 obj 的规范 JSON 字符串（键排序、无空白、保留非 ASCII 原文）。"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
