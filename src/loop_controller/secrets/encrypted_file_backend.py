"""EncryptedFileSecretBackend：加密落盘的文件 Secret 后端（v0.24.0）。

与 FileSecretBackend 的目录结构相同，但 JSON 文件中的 value 字段是密文：

    {
      "value": "base64(ciphertext)",
      "encrypted": true,
      "version": "1",
      "expires_at": "2026-12-31T23:59:59Z"
    }

加密方案：AES-256-GCM，密钥从环境变量 `LC_SECRET_ENCRYPTION_KEY` 读取。
密钥必须是 32 字节，支持 hex（64 字符）或 base64 编码。
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from loop_controller.secrets.exceptions import SecretError
from loop_controller.secrets.file_backend import FileSecretBackend
from loop_controller.secrets.models import SecretRef, SecretScope, SecretValue

logger = logging.getLogger(__name__)


class EncryptedFileSecretBackend(FileSecretBackend):
    """加密文件后端。

    继承 FileSecretBackend 的目录扫描、过期校验、权限校验逻辑，
    仅覆写 secret 解析逻辑以支持 AES-GCM 解密。
    """

    def __init__(
        self,
        base_path: str | Path,
        *,
        key_env: str = "LC_SECRET_ENCRYPTION_KEY",
    ) -> None:
        self._key = self._resolve_encryption_key(key_env)
        super().__init__(base_path)

    # ------------------------------------------------------------------
    # 覆写解析逻辑
    # ------------------------------------------------------------------

    def _parse_secret(self, path: Path) -> SecretValue:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SecretError(f"JSON 解析失败：{exc}") from exc
        if not isinstance(raw, dict):
            raise SecretError("secret 文件顶层必须是对象")

        encrypted = raw.get("encrypted", False)
        value = raw.get("value")
        if value is None:
            raise SecretError("secret 文件缺少 value 字段")

        if encrypted:
            plaintext = self._decrypt(str(value))
            try:
                value = json.loads(plaintext)
            except json.JSONDecodeError:
                # 允许非 JSON 字符串作为 secret value
                value = plaintext

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

    # ------------------------------------------------------------------
    # 加密工具（可被 CLI/管理脚本复用）
    # ------------------------------------------------------------------

    def _decrypt(self, ciphertext_b64: str) -> str:
        """解密 base64(nonce || ciphertext || tag) 为 UTF-8 明文。"""
        try:
            payload = base64.b64decode(ciphertext_b64, validate=True)
        except binascii.Error as exc:
            raise SecretError(f"密文 base64 解码失败：{exc}") from exc
        if len(payload) < 13:
            raise SecretError("密文格式非法：长度不足")
        nonce = payload[:12]
        ciphertext = payload[12:]
        aesgcm = AESGCM(self._key)
        try:
            plaintext = aesgcm.decrypt(nonce, ciphertext, None)
        except Exception as exc:  # noqa: BLE001
            raise SecretError(f"AES-GCM 解密失败：{exc}") from exc
        return plaintext.decode("utf-8")

    @staticmethod
    def encrypt(plaintext: str, key: bytes) -> str:
        """加密明文并返回 base64(nonce || ciphertext || tag)。

        供管理脚本或测试使用。
        """
        aesgcm = AESGCM(key)
        nonce = os.urandom(12)
        ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
        return base64.b64encode(nonce + ciphertext).decode("ascii")

    @staticmethod
    def _resolve_encryption_key(env_name: str) -> bytes:
        raw = os.environ.get(env_name)
        if not raw:
            raise SecretError(
                f"加密 Secret 后端需要环境变量 {env_name} 提供 32 字节密钥"
            )
        raw = raw.strip()
        # 优先 hex（64 字符）
        if len(raw) == 64:
            try:
                key = bytes.fromhex(raw)
                if len(key) == 32:
                    return key
            except ValueError:
                pass
        # 再试 base64
        try:
            key = base64.b64decode(raw, validate=True)
        except binascii.Error as exc:
            raise SecretError(
                f"{env_name} 无法解析为 hex 或 base64：{exc}"
            ) from exc
        if len(key) != 32:
            raise SecretError(
                f"{env_name} 解码后长度 {len(key)} 字节，必须为 32 字节"
            )
        return key

    # ------------------------------------------------------------------
    # 兼容 SecretBroker 协议：list/reload 已由父类实现
    # ------------------------------------------------------------------

    async def get(self, ref: SecretRef) -> SecretValue | None:
        return await super().get(ref)

    async def list(
        self, scope: SecretScope, tenant_id: str | None = None
    ) -> list[str]:
        return await super().list(scope, tenant_id)

    async def reload(self) -> None:
        await super().reload()
