"""HTTPClient 响应大小限制测试（v0.23.1）。"""

from __future__ import annotations

import httpx
import pytest

from loop_controller.executors.http_client import HTTPClient
from loop_controller.executors.http_security import HTTPSecurityError


def _mock_transport_with_body(body: bytes) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    return httpx.MockTransport(handler)


def _mock_transport_with_chunked_body(body: bytes) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        # 使用 ByteStream 且不显式 Content-Length，强制走流式读取路径。
        return httpx.Response(
            200,
            stream=httpx.ByteStream(body),
            headers={"Content-Type": "application/octet-stream"},
        )

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_streaming_response_size_limit() -> None:
    """流式大响应应在超过 max_response_size 时被截断并抛错。"""
    large_body = b"x" * (1024 + 1)
    client = HTTPClient(
        max_response_size=1024,
        transport=_mock_transport_with_chunked_body(large_body),
    )
    with pytest.raises(HTTPSecurityError) as exc_info:
        async with client:
            await client.request("GET", "https://allowed.example.com/data")
    assert exc_info.value.error_code == "http_response_too_large"


@pytest.mark.asyncio
async def test_content_length_pre_check_blocks_oversized() -> None:
    """Content-Length 超过限制时无需读取流直接拒绝。"""
    body = b"x" * 100
    client = HTTPClient(
        max_response_size=50,
        transport=_mock_transport_with_body(body),
    )
    with pytest.raises(HTTPSecurityError) as exc_info:
        async with client:
            await client.request("GET", "https://allowed.example.com/data")
    assert exc_info.value.error_code == "http_response_too_large"


@pytest.mark.asyncio
async def test_small_response_decoded_ok() -> None:
    body = b'{"ok": true}'
    client = HTTPClient(
        max_response_size=1024,
        transport=_mock_transport_with_body(body),
    )
    async with client:
        status, _headers, text, _elapsed = await client.request(
            "GET", "https://allowed.example.com/data"
        )
    assert status == 200
    assert text == '{"ok": true}'
