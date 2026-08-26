"""受控 HTTP 客户端：超时、重定向、响应大小限制（v0.21.0）。"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from loop_controller.executors.http_security import HTTPSecurityError


class HTTPClient:
    """Loop Controller HTTP Executor 使用的受控 httpx 客户端。

    功能：
    - 统一超时、连接池、重定向控制；
    - 拦截并校验重定向后的 URL（防 SSRF）；
    - 限制响应体大小；
    - 返回 (status_code, headers, text, elapsed_ms)。
    """

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        max_redirects: int = 3,
        max_response_size: int = 5 * 1024 * 1024,
        verify_ssl: bool = True,
        limits: httpx.Limits | None = None,
        transport: Any | None = None,
    ) -> None:
        self._timeout = timeout
        self._max_redirects = max_redirects
        self._max_response_size = max_response_size
        self._verify_ssl = verify_ssl
        self._limits = limits or httpx.Limits(
            max_connections=100, max_keepalive_connections=20
        )
        # 允许注入 transport（主要用于测试），否则使用默认 AsyncHTTPTransport。
        self._transport = transport or httpx.AsyncHTTPTransport(limits=self._limits)
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> HTTPClient:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def start(self) -> None:
        """启动底层 httpx.AsyncClient。"""
        if self._client is not None:
            return
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout),
            follow_redirects=False,  # 我们手动处理以校验每个中间 URL
            verify=self._verify_ssl,
            transport=self._transport,
        )

    async def aclose(self) -> None:
        """关闭底层客户端。"""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        body: dict[str, Any] | str | None = None,
        *,
        url_checker: Any | None = None,
    ) -> tuple[int, dict[str, str], str, float]:
        """发送 HTTP 请求并返回响应摘要。

        Args:
            method: HTTP 方法。
            url: 目标 URL（已通过安全校验）。
            headers: 请求头。
            body: 请求体；dict 会 JSON 序列化，str 直接发送。
            url_checker: 可选可调用对象，用于校验重定向后的 URL。

        Returns:
            (status_code, response_headers, response_text, elapsed_ms)
        """
        if self._client is None:
            await self.start()
        assert self._client is not None

        request_headers = dict(headers) if headers else {}
        content: str | bytes | None = None
        if body is not None:
            if isinstance(body, dict):
                content = json.dumps(body, ensure_ascii=False)
                request_headers.setdefault("Content-Type", "application/json")
            else:
                content = body

        elapsed_ms = 0.0
        current_url = url
        redirects = 0
        while True:
            start = time.perf_counter()
            try:
                response = await self._client.request(
                    method,
                    current_url,
                    headers=request_headers,
                    content=content,
                )
            except httpx.TimeoutException as exc:
                raise HTTPSecurityError(
                    f"HTTP 请求超时: {exc}", "http_timeout"
                ) from exc
            except httpx.ConnectError as exc:
                raise HTTPSecurityError(
                    f"HTTP 连接失败: {exc}", "http_connect_error"
                ) from exc
            except httpx.NetworkError as exc:
                raise HTTPSecurityError(
                    f"HTTP 网络错误: {exc}", "http_network_error"
                ) from exc
            finally:
                elapsed_ms += (time.perf_counter() - start) * 1000

            # 手动处理重定向，便于 SSRF 校验
            if response.status_code in (301, 302, 303, 307, 308):
                redirects += 1
                if redirects > self._max_redirects:
                    raise HTTPSecurityError(
                        f"重定向次数超过 {self._max_redirects}", "http_too_many_redirects"
                    )
                location = response.headers.get("location")
                if not location:
                    break
                current_url = str(response.url.join(location))
                if url_checker is not None:
                    url_checker(current_url)
                if response.status_code == 303:
                    method = "GET"
                    content = None
                # 继续循环发送请求到新 URL
                continue

            break

        # 限制响应大小：优先通过 Content-Length 预检，再流式读取避免一次性加载大响应。
        content_length = response.headers.get("content-length")
        if content_length:
            try:
                parsed_length = int(content_length)
            except ValueError:
                parsed_length = None
            if parsed_length is not None and parsed_length > self._max_response_size:
                await response.aclose()
                raise HTTPSecurityError(
                    f"响应体超过 {self._max_response_size} 字节限制",
                    "http_response_too_large",
                )

        content_chunks: list[bytes] = []
        total_size = 0
        async for chunk in response.aiter_bytes():
            content_chunks.append(chunk)
            total_size += len(chunk)
            if total_size > self._max_response_size:
                await response.aclose()
                raise HTTPSecurityError(
                    f"响应体超过 {self._max_response_size} 字节限制",
                    "http_response_too_large",
                )
        content_bytes = b"".join(content_chunks)

        text = content_bytes.decode("utf-8", errors="replace")
        response_headers = dict(response.headers)
        return response.status_code, response_headers, text, elapsed_ms
