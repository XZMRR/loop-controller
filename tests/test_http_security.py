"""HTTP Executor 安全策略测试（v0.21.0）。

覆盖 SSRF 防护、allowed_hosts 白名单、通配符匹配。
"""

from __future__ import annotations

import pytest

from loop_controller.executors.http_security import HTTPSecurityError, HTTPSecurityPolicy


class TestHTTPSecurityPolicy:
    """HTTPSecurityPolicy 测试。"""

    def test_allow_public_host(self) -> None:
        # 默认已启用 DNS 解析校验；此测试不依赖网络，显式关闭 DNS 解析。
        policy = HTTPSecurityPolicy(
            ["company.atlassian.net"], require_dns_resolution=False
        )
        policy.check_url("https://company.atlassian.net/rest/api/3/issue")

    def test_block_localhost(self) -> None:
        policy = HTTPSecurityPolicy(["localhost"], require_dns_resolution=False)
        with pytest.raises(HTTPSecurityError) as exc_info:
            policy.check_url("http://localhost:8080/api")
        assert exc_info.value.error_code == "http_security_blocked"

    def test_block_private_ip(self) -> None:
        policy = HTTPSecurityPolicy(["10.0.0.1"], require_dns_resolution=False)
        with pytest.raises(HTTPSecurityError) as exc_info:
            policy.check_url("http://10.0.0.1/api")
        assert exc_info.value.error_code == "http_security_blocked"

    def test_block_127_ip(self) -> None:
        policy = HTTPSecurityPolicy(["127.0.0.1"], require_dns_resolution=False)
        with pytest.raises(HTTPSecurityError) as exc_info:
            policy.check_url("http://127.0.0.1:8080/api")
        assert exc_info.value.error_code == "http_security_blocked"

    def test_block_localhost_substring(self) -> None:
        # my-localhost 等子串不应被误伤。
        policy = HTTPSecurityPolicy(
            ["my-localhost.example.com"], require_dns_resolution=False
        )
        policy.check_url("https://my-localhost.example.com/api")

    def test_host_not_in_allowlist(self) -> None:
        policy = HTTPSecurityPolicy(
            ["company.atlassian.net"], require_dns_resolution=False
        )
        with pytest.raises(HTTPSecurityError) as exc_info:
            policy.check_url("https://attacker.com/api")
        assert "不在 allowed_hosts" in str(exc_info.value)

    def test_wildcard_allowlist(self) -> None:
        policy = HTTPSecurityPolicy(
            ["*.atlassian.net"], require_dns_resolution=False
        )
        policy.check_url("https://company.atlassian.net/api")

    def test_wildcard_mismatch(self) -> None:
        policy = HTTPSecurityPolicy(
            ["*.atlassian.net"], require_dns_resolution=False
        )
        with pytest.raises(HTTPSecurityError):
            policy.check_url("https://atlassian.com/api")

    def test_is_local_or_private(self) -> None:
        assert HTTPSecurityPolicy.is_local_or_private("http://127.0.0.1/a") is True
        assert HTTPSecurityPolicy.is_local_or_private("http://localhost/a") is True
        assert HTTPSecurityPolicy.is_local_or_private("http://10.0.0.1/a") is True
        assert HTTPSecurityPolicy.is_local_or_private("https://example.com/a") is False

    def test_disable_private_block_allows_ip(self) -> None:
        policy = HTTPSecurityPolicy(
            ["10.0.0.1"], block_private=False, require_dns_resolution=False
        )
        # 仍然需要命中 allowlist，但不检查私有段
        policy.check_url("http://10.0.0.1/api")


class TestHTTPSecurityDNSResolution:
    """DNS 解析校验测试。"""

    def test_default_enables_dns_resolution(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """默认启用 DNS 解析后二次校验，可阻止 DNS 重绑定。"""
        import socket

        def _fake_getaddrinfo(host, port, *args, **kwargs):
            if host == "allowed.example.com":
                return [(None, None, None, None, ("10.0.0.1", port))]
            raise socket.gaierror("not found")

        monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)
        policy = HTTPSecurityPolicy(["allowed.example.com"])
        with pytest.raises(HTTPSecurityError) as exc_info:
            policy.check_url("https://allowed.example.com/api")
        assert exc_info.value.error_code == "http_security_blocked"

    def test_localhost_resolved_blocked(self) -> None:
        policy = HTTPSecurityPolicy(
            ["localhost"], require_dns_resolution=True
        )
        with pytest.raises(HTTPSecurityError) as exc_info:
            policy.check_url("http://localhost:8080/")
        assert exc_info.value.error_code in ("http_security_blocked", "http_dns_error")
