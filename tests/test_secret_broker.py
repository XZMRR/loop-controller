"""Secret Broker 单元测试（v0.22.0）。"""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from loop_controller.secrets import FileSecretBackend, MemorySecretBackend
from loop_controller.secrets.exceptions import SecretNotFoundError
from loop_controller.secrets.models import SecretRef, SecretScope


class TestMemorySecretBackend:
    """内存后端测试。"""

    async def test_get_global_secret(self) -> None:
        backend = MemorySecretBackend()
        backend.put("api_key", "secret123")
        value = await backend.get(SecretRef(name="api_key"))
        assert value is not None
        assert value.value == "secret123"
        assert value.scope == SecretScope.GLOBAL

    async def test_get_tenant_secret(self) -> None:
        backend = MemorySecretBackend()
        backend.put("api_key", "global")
        backend.put("api_key", "tenant", tenant_id="acme")

        global_value = await backend.get(SecretRef(name="api_key"))
        assert global_value is not None
        assert global_value.value == "global"

        tenant_value = await backend.get(SecretRef(name="api_key", tenant_id="acme"))
        assert tenant_value is not None
        assert tenant_value.value == "tenant"

    async def test_extract_key_from_dict_secret(self) -> None:
        backend = MemorySecretBackend()
        backend.put("creds", {"username": "u", "password": "p"})
        value = await backend.get(SecretRef(name="creds", key="username"))
        assert value is not None
        assert value.value == "u"

    async def test_missing_key_raises(self) -> None:
        backend = MemorySecretBackend()
        backend.put("creds", {"username": "u"})
        with pytest.raises(SecretNotFoundError):
            await backend.get(SecretRef(name="creds", key="password"))

    async def test_version_mismatch_returns_none(self) -> None:
        backend = MemorySecretBackend()
        backend.put("api_key", "secret123")
        value = await backend.get(SecretRef(name="api_key", version="2"))
        assert value is None

    async def test_list_global(self) -> None:
        backend = MemorySecretBackend()
        backend.put("a", "1")
        backend.put("b", "2")
        names = await backend.list(SecretScope.GLOBAL)
        assert names == ["a", "b"]


class TestFileSecretBackend:
    """文件后端测试。"""

    def test_global_secret(self, tmp_path: Path) -> None:
        base = tmp_path / "secrets"
        global_dir = base / "global"
        global_dir.mkdir(parents=True)
        (global_dir / "api_key.json").write_text(
            json.dumps({"value": "global-secret", "version": "1"}),
            encoding="utf-8",
        )
        backend = FileSecretBackend(base)
        value = backend._get_global("api_key", None)  # 同步方法可直接测
        assert value is not None
        assert value.value == "global-secret"

    def test_tenant_fallback_to_global(self, tmp_path: Path) -> None:
        base = tmp_path / "secrets"
        global_dir = base / "global"
        global_dir.mkdir(parents=True)
        (global_dir / "api_key.json").write_text(
            json.dumps({"value": "global-secret"}),
            encoding="utf-8",
        )
        backend = FileSecretBackend(base)
        ref = SecretRef(name="api_key", tenant_id="acme")
        value = backend._get_from_tenant(ref)
        assert value is None
        value = backend._get_global("api_key", None)
        assert value is not None
        assert value.value == "global-secret"

    def test_tenant_secret_overrides_global(self, tmp_path: Path) -> None:
        base = tmp_path / "secrets"
        global_dir = base / "global"
        global_dir.mkdir(parents=True)
        (global_dir / "api_key.json").write_text(
            json.dumps({"value": "global-secret"}), encoding="utf-8"
        )
        tenant_dir = base / "tenants" / "acme"
        tenant_dir.mkdir(parents=True)
        (tenant_dir / "api_key.json").write_text(
            json.dumps({"value": "tenant-secret"}), encoding="utf-8"
        )
        backend = FileSecretBackend(base)
        ref = SecretRef(name="api_key", tenant_id="acme")
        value = backend._resolve_key(backend._get_from_tenant(ref), ref)
        assert value is not None
        assert value.value == "tenant-secret"

    def test_key_extraction(self, tmp_path: Path) -> None:
        base = tmp_path / "secrets"
        global_dir = base / "global"
        global_dir.mkdir(parents=True)
        (global_dir / "creds.json").write_text(
            json.dumps({"value": {"username": "u", "password": "p"}}),
            encoding="utf-8",
        )
        backend = FileSecretBackend(base)
        value = backend._get_global("creds", "username")
        assert value is not None
        assert value.value == "u"

    def test_expired_secret_not_loaded(self, tmp_path: Path) -> None:
        base = tmp_path / "secrets"
        global_dir = base / "global"
        global_dir.mkdir(parents=True)
        expired = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        (global_dir / "expired.json").write_text(
            json.dumps({"value": "x", "expires_at": expired}),
            encoding="utf-8",
        )
        backend = FileSecretBackend(base)
        assert "expired" not in backend._cache

    def test_world_readable_file_rejected(self, tmp_path: Path) -> None:
        if os.name == "nt":
            pytest.skip("文件权限检查仅在 Unix 系统生效")
        base = tmp_path / "secrets"
        global_dir = base / "global"
        global_dir.mkdir(parents=True)
        path = global_dir / "api_key.json"
        path.write_text(json.dumps({"value": "x"}), encoding="utf-8")
        current = path.stat().st_mode
        path.chmod(current | stat.S_IROTH)

        backend = FileSecretBackend(base)
        assert "api_key" not in backend._cache

    async def test_reload_refreshes_cache(self, tmp_path: Path) -> None:
        base = tmp_path / "secrets"
        global_dir = base / "global"
        global_dir.mkdir(parents=True)
        path = global_dir / "api_key.json"
        path.write_text(json.dumps({"value": "v1"}), encoding="utf-8")

        backend = FileSecretBackend(base)
        assert (await backend.get(SecretRef(name="api_key"))).value == "v1"

        path.write_text(json.dumps({"value": "v2"}), encoding="utf-8")
        await backend.reload()
        assert (await backend.get(SecretRef(name="api_key"))).value == "v2"
