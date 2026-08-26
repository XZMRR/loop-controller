"""EncryptedFileSecretBackend 测试（v0.24.0）。"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest

from loop_controller.secrets import EncryptedFileSecretBackend, SecretRef
from loop_controller.secrets.exceptions import SecretError


@pytest.fixture
def key() -> bytes:
    return os.urandom(32)


@pytest.fixture
def base_path(tmp_path: Path) -> Path:
    return tmp_path / "secrets"


def _write_encrypted(base: Path, name: str, plaintext: str, key: bytes) -> None:
    global_dir = base / "global"
    global_dir.mkdir(parents=True)
    ciphertext = EncryptedFileSecretBackend.encrypt(plaintext, key)
    path = global_dir / f"{name}.json"
    path.write_text(
        json.dumps(
            {
                "value": ciphertext,
                "encrypted": True,
                "version": "1",
            }
        ),
        encoding="utf-8",
    )
    # 非 Windows 下限制权限，避免 FileSecretBackend 拒绝加载
    if os.name != "nt":
        os.chmod(path, 0o600)


@pytest.mark.asyncio
async def test_decrypt_encrypted_secret(base_path: Path, key: bytes, monkeypatch) -> None:
    _write_encrypted(base_path, "api_key", json.dumps("secret-value"), key)
    monkeypatch.setenv("LC_SECRET_ENCRYPTION_KEY", key.hex())

    backend = EncryptedFileSecretBackend(base_path)
    value = await backend.get(SecretRef(name="api_key"))
    assert value is not None
    assert value.value == "secret-value"


def test_missing_key_raises(base_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("LC_SECRET_ENCRYPTION_KEY", raising=False)
    with pytest.raises(SecretError, match="LC_SECRET_ENCRYPTION_KEY"):
        EncryptedFileSecretBackend(base_path)


@pytest.mark.asyncio
async def test_plaintext_secret_still_works(base_path: Path, monkeypatch) -> None:
    global_dir = base_path / "global"
    global_dir.mkdir(parents=True)
    path = global_dir / "plain.json"
    path.write_text(json.dumps({"value": "plain-value"}), encoding="utf-8")
    if os.name != "nt":
        os.chmod(path, 0o600)

    key = os.urandom(32)
    monkeypatch.setenv("LC_SECRET_ENCRYPTION_KEY", key.hex())
    backend = EncryptedFileSecretBackend(base_path)
    value = await backend.get(SecretRef(name="plain"))
    assert value is not None
    assert value.value == "plain-value"


def test_invalid_key_length(base_path: Path, monkeypatch) -> None:
    # 使用合法 base64 但解码后不是 32 字节
    invalid_key = base64.b64encode(b"short").decode("ascii")
    monkeypatch.setenv("LC_SECRET_ENCRYPTION_KEY", invalid_key)
    with pytest.raises(SecretError, match="32 字节"):
        EncryptedFileSecretBackend(base_path)
