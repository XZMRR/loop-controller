"""FileSecretBackend：从文件系统按 global/tenant 命名空间加载 secret（v0.22.0）。"""

from __future__ import annotations

import json
import logging
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loop_controller.secrets.exceptions import SecretError, SecretNotFoundError
from loop_controller.secrets.models import SecretRef, SecretScope, SecretValue

logger = logging.getLogger(__name__)


class FileSecretBackend:
    """默认文件后端。

    目录结构::

        {base_path}/global/{name}.json
        {base_path}/tenants/{tenant_id}/{name}.json

    JSON 文件格式::

        {"value": "...", "version": "1", "expires_at": "2026-12-31T23:59:59Z"}

    安全：加载时校验文件权限，若 world-readable（o+r）则拒绝加载并告警。
    """

    def __init__(self, base_path: str | Path) -> None:
        self._base = Path(base_path)
        self._cache: dict[str, SecretValue] = {}
        self._tenant_cache: dict[str, dict[str, SecretValue]] = {}
        self._load_all()

    @property
    def base_path(self) -> Path:
        """返回 secret 文件根目录，供热更新等模块使用。"""
        return self._base

    # ------------------------------------------------------------------
    # SecretBroker Protocol
    # ------------------------------------------------------------------

    async def get(self, ref: SecretRef) -> SecretValue | None:
        """按引用读取 secret；优先 tenant，未命中 fallback 到 global。"""
        value = self._get_from_tenant(ref)
        if value is not None:
            return value
        return self._get_global(ref.name, ref.key)

    async def list(
        self, scope: SecretScope, tenant_id: str | None = None
    ) -> list[str]:
        """列出某作用域下的 secret 名称。"""
        if scope == SecretScope.TENANT:
            if tenant_id is None:
                return []
            return sorted(self._tenant_cache.get(tenant_id, {}).keys())
        return sorted(self._cache.keys())

    async def reload(self) -> None:
        """重新扫描文件系统并刷新缓存。"""
        self._cache.clear()
        self._tenant_cache.clear()
        self._load_all()

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _load_all(self) -> None:
        self._load_scope(self._base / "global", self._cache)
        tenants_dir = self._base / "tenants"
        if not tenants_dir.exists():
            return
        for tenant_dir in tenants_dir.iterdir():
            if not tenant_dir.is_dir():
                continue
            tenant_id = tenant_dir.name
            tenant_cache: dict[str, SecretValue] = {}
            self._load_scope(tenant_dir, tenant_cache)
            if tenant_cache:
                self._tenant_cache[tenant_id] = tenant_cache

    def _load_scope(self, scope_dir: Path, cache: dict[str, SecretValue]) -> None:
        if not scope_dir.exists():
            return
        for path in scope_dir.iterdir():
            if not path.is_file() or path.suffix != ".json":
                continue
            try:
                self._check_permissions(path)
                secret = self._parse_secret(path)
            except SecretError as exc:
                logger.warning("Secret 文件 %s 加载失败：%s", path, exc)
                continue
            name = path.stem
            cache[name] = secret

    def _check_permissions(self, path: Path) -> None:
        """拒绝 world-readable 的 secret 文件（仅 Unix 系统生效）。"""
        if os.name == "nt":
            return
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except OSError as exc:
            raise SecretError(f"无法读取文件权限：{exc}") from exc
        if mode & stat.S_IROTH:
            raise SecretError(
                f"secret 文件 {path} 权限过宽（world-readable），拒绝加载"
            )

    def _parse_secret(self, path: Path) -> SecretValue:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SecretError(f"JSON 解析失败：{exc}") from exc
        if not isinstance(raw, dict):
            raise SecretError("secret 文件顶层必须是对象")

        value = raw.get("value")
        if value is None:
            raise SecretError("secret 文件缺少 value 字段")

        scope_name = raw.get("scope", "global")
        tenant_id = raw.get("tenant_id")
        expires_at = self._parse_expires(raw.get("expires_at"))

        secret = SecretValue(
            value=value,
            scope=SecretScope(scope_name),
            tenant_id=tenant_id,
            version=str(raw.get("version", "1")),
            expires_at=expires_at,
            metadata=raw.get("metadata", {}),
        )
        if secret.is_expired():
            raise SecretError(f"secret {path.stem} 已过期")
        return secret

    @staticmethod
    def _parse_expires(value: Any) -> datetime | None:
        if not value:
            return None
        if isinstance(value, datetime):
            # YAML 可能解析为 datetime；确保带 tz
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value
        if isinstance(value, str):
            # 兼容 ISO-8601，含或不含 Z
            text = value.replace("Z", "+00:00")
            try:
                parsed = datetime.fromisoformat(text)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed
            except ValueError as exc:
                raise SecretError(f"expires_at 格式非法：{exc}") from exc
        raise SecretError("expires_at 必须是字符串或 datetime")

    def _get_from_tenant(self, ref: SecretRef) -> SecretValue | None:
        tenant_id = ref.tenant_id
        if tenant_id is None:
            return None
        cache = self._tenant_cache.get(tenant_id)
        if cache is None:
            return None
        return self._resolve_key(cache.get(ref.name), ref)

    def _get_global(self, name: str, key: str | None) -> SecretValue | None:
        return self._resolve_key(self._cache.get(name), SecretRef(name=name, key=key))

    def _resolve_key(
        self, secret: SecretValue | None, ref: SecretRef
    ) -> SecretValue | None:
        if secret is None:
            return None
        if ref.version is not None and secret.version != ref.version:
            return None
        if ref.key is None:
            return secret
        if not isinstance(secret.value, dict):
            raise SecretNotFoundError(
                f"secret {ref.name} 不是对象，无法提取 key {ref.key}",
                ref_name=ref.name,
            )
        if ref.key not in secret.value:
            raise SecretNotFoundError(
                f"secret {ref.name} 缺少 key {ref.key}",
                ref_name=ref.name,
            )
        return secret.model_copy(update={"value": secret.value[ref.key]})
