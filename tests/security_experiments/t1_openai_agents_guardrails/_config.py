"""
共享配置：初始化 OpenAI Agents SDK 的默认客户端。

支持 OpenAI 官方 API 和 OpenAI 兼容 API（如 Kimi、DeepSeek、Azure 等）。
"""

import os

from agents import (
    set_default_openai_api,
    set_default_openai_client,
    set_tracing_disabled,
)
from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()


def init_openai_client():
    """根据环境变量初始化默认 OpenAI 客户端。"""
    api_key = os.getenv("OPENAI_API_KEY")
    base_url = os.getenv("OPENAI_BASE_URL")

    if not api_key:
        raise ValueError(
            "未找到 OPENAI_API_KEY 环境变量。请复制 .env.example 为 .env 并填入 API Key。"
        )

    kwargs = {"api_key": api_key, "timeout": 60.0}
    if base_url:
        kwargs["base_url"] = base_url

    client = AsyncOpenAI(**kwargs)
    set_default_openai_client(client)

    # 如果使用非 OpenAI 官方服务（如 Kimi），需要切换到 Chat Completions API
    # OpenAI Agents SDK 默认使用 Responses API，很多第三方服务不支持
    if base_url and "openai.com" not in base_url:
        set_default_openai_api("chat_completions")

    # 禁用 OpenAI 平台 Tracing，避免第三方 API 无 tracing 权限时报错
    set_tracing_disabled(True)

    return client


# 模块导入时自动初始化
init_openai_client()
