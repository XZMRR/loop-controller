"""HTTP Executor 安全策略：SSRF 防护、域名白名单、URL 校验（v0.21.0）。"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlparse


class HTTPSecurityError(Exception):
    """HTTP 安全策略拒绝。"""

    def __init__(self, reason: str, error_code: str = "http_security_blocked"):
        super().__init__(reason)
        self.reason = reason
        self.error_code = error_code


class HTTPSecurityPolicy:
    """HTTP 工具安全策略。

    默认拒绝访问本地/内网地址，并要求目标 host 命中工具声明的 ``allowed_hosts``。
    """

    # 默认拒绝的私有/特殊地址段
    _BLOCKED_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
        ipaddress.ip_network("127.0.0.0/8"),
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
        ipaddress.ip_network("169.254.0.0/16"),
        ipaddress.ip_network("0.0.0.0/8"),
        ipaddress.ip_network("::1/128"),
        ipaddress.ip_network("fc00::/7"),   # 唯一本地地址
        ipaddress.ip_network("fe80::/10"),  # 链路本地
    )

    # 仅当 host 整个字符串为 localhost 时才拒绝，避免 my-localhost 之类误伤。
    _LOCALHOST_RE = re.compile(r"^localhost$", re.IGNORECASE)

    def __init__(
        self,
        allowed_hosts: list[str],
        *,
        block_private: bool = True,
        # v0.23.1：默认启用 DNS 解析后二次校验，降低 DNS 重绑定绕过风险。
        require_dns_resolution: bool = True,
    ) -> None:
        self._allowed_hosts = {h.lower() for h in allowed_hosts}
        self._block_private = block_private
        self._require_dns_resolution = require_dns_resolution

    def check_url(self, url: str) -> None:
        """校验 URL 是否允许访问；不通过则抛 HTTPSecurityError。"""
        parsed = urlparse(url)
        host = parsed.hostname
        if not host:
            raise HTTPSecurityError("URL 缺少 host", "http_invalid_url")

        # 显式 localhost 字符串拒绝
        if self._LOCALHOST_RE.search(host):
            raise HTTPSecurityError(
                f"localhost 不允许: {host}", "http_security_blocked"
            )

        # 白名单检查（支持精确主机或通配符 *.example.com）
        if not self._host_allowed(host):
            raise HTTPSecurityError(
                f"host {host!r} 不在 allowed_hosts 中",
                "http_security_blocked",
            )

        # 私有/内网 IP 检查
        if self._block_private:
            self._check_ip_not_private(host)

    def _host_allowed(self, host: str) -> bool:
        host_lower = host.lower()
        if host_lower in self._allowed_hosts:
            return True
        # 支持 *.example.com 通配符
        parts = host_lower.split(".")
        for i in range(1, len(parts)):
            wildcard = "*." + ".".join(parts[i:])
            if wildcard in self._allowed_hosts:
                return True
        return False

    def _check_ip_not_private(self, host: str) -> None:
        """如果 host 是 IP，检查是否在私有段；如果是域名，可选 DNS 解析后检查。"""
        try:
            addr = ipaddress.ip_address(host)
            if self._is_blocked_network(addr):
                raise HTTPSecurityError(
                    f"IP {host} 属于禁止访问的私有/本地地址段",
                    "http_security_blocked",
                )
        except ValueError:
            # 域名，可选解析后检查
            if self._require_dns_resolution:
                self._check_resolved_ips(host)

    def _check_resolved_ips(self, host: str) -> None:
        """解析域名并检查所有解析结果是否都是公网 IP。"""
        import socket

        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror as exc:
            raise HTTPSecurityError(
                f"无法解析域名 {host}: {exc}", "http_dns_error"
            ) from exc

        seen = set()
        for info in infos:
            ip_str = info[4][0]
            if ip_str in seen:
                continue
            seen.add(ip_str)
            addr = ipaddress.ip_address(ip_str)
            if self._is_blocked_network(addr):
                raise HTTPSecurityError(
                    f"域名 {host} 解析到私有/本地 IP {ip_str}",
                    "http_security_blocked",
                )

    def _is_blocked_network(
        self, addr: ipaddress.IPv4Address | ipaddress.IPv6Address
    ) -> bool:
        for network in self._BLOCKED_NETWORKS:
            if addr in network:
                return True
        return False

    @staticmethod
    def is_local_or_private(url: str) -> bool:
        """便捷方法：判断 URL 是否指向本地/私有地址（不抛异常）。"""
        parsed = urlparse(url)
        host = parsed.hostname
        if not host:
            return False
        if HTTPSecurityPolicy._LOCALHOST_RE.search(host):
            return True
        try:
            addr = ipaddress.ip_address(host)
            for network in HTTPSecurityPolicy._BLOCKED_NETWORKS:
                if addr in network:
                    return True
        except ValueError:
            pass
        return False
