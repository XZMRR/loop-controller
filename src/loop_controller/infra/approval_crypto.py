"""ApprovalStore 敏感载荷加密（v0.36.1）。

使用 AES-256-GCM 加密 ``ApprovalRequest`` 中的 ``tool_arguments`` 和
``original_decision``，AAD 绑定 ``request_id / call_id / agent_id / tool_name / schema_version``。
密钥通过环境变量 ``LC_APPROVAL_ENCRYPTION_KEY`` 提供，支持 hex（64 字符）或 base64。
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
from typing import Any, cast

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger(__name__)


class ApprovalCryptoError(Exception):
    """Approval 加密/解密失败（fail-closed）。"""


class ApprovalCrypto:
    """AES-256-GCM 加密工具，专门用于审批存储敏感载荷。"""

    def __init__(self, key: bytes | None = None, *, key_env: str = "LC_APPROVAL_ENCRYPTION_KEY") -> None:
        self._key = key if key is not None else self._resolve_key(key_env)

    @classmethod
    def from_env(cls, key_env: str = "LC_APPROVAL_ENCRYPTION_KEY") -> ApprovalCrypto:
        """从环境变量构造，缺失或非法时抛出 ``ApprovalCryptoError``（fail-closed）。"""
        return cls(key_env=key_env)

    @classmethod
    def from_env_or_none(
        cls, key_env: str = "LC_APPROVAL_ENCRYPTION_KEY"
    ) -> ApprovalCrypto | None:
        """环境变量存在且合法时构造加密器，否则返回 ``None``（明文兼容模式）。"""
        if os.environ.get(key_env):
            return cls(key_env=key_env)
        return None

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def encrypt(self, plaintext_payload: dict[str, Any], aad_context: dict[str, str]) -> str:
        """加密敏感载荷，返回 ``base64(nonce || ciphertext || tag)``。"""
        aad = self._canonical_aad(aad_context)
        plaintext = json.dumps(plaintext_payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        aesgcm = AESGCM(self._key)
        nonce = os.urandom(12)
        ciphertext = aesgcm.encrypt(nonce, plaintext, aad)
        return base64.b64encode(nonce + ciphertext).decode("ascii")

    def decrypt(self, ciphertext_b64: str, aad_context: dict[str, str]) -> dict[str, Any]:
        """解密并验证 AAD，返回敏感载荷字典。"""
        try:
            payload = base64.b64decode(ciphertext_b64, validate=True)
        except binascii.Error as exc:
            raise ApprovalCryptoError(f"审批密文 base64 解码失败：{exc}") from exc
        if len(payload) < 13:
            raise ApprovalCryptoError("审批密文格式非法：长度不足")
        nonce = payload[:12]
        ciphertext = payload[12:]
        aad = self._canonical_aad(aad_context)
        aesgcm = AESGCM(self._key)
        try:
            plaintext = aesgcm.decrypt(nonce, ciphertext, aad)
        except Exception as exc:  # noqa: BLE001
            raise ApprovalCryptoError(f"审批 AES-GCM 解密/认证失败：{exc}") from exc
        try:
            return cast(dict[str, Any], json.loads(plaintext.decode("utf-8")))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ApprovalCryptoError(f"审批密文解密后不是合法 JSON：{exc}") from exc

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def _canonical_aad(context: dict[str, str]) -> bytes:
        """AAD 使用键排序后的规范 JSON，确保绑定上下文字段。"""
        return json.dumps(context, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    @staticmethod
    def _resolve_key(env_name: str) -> bytes:
        raw = os.environ.get(env_name)
        if not raw:
            raise ApprovalCryptoError(
                f"审批加密需要环境变量 {env_name} 提供 32 字节密钥"
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
            raise ApprovalCryptoError(
                f"{env_name} 无法解析为 hex 或 base64：{exc}"
            ) from exc
        if len(key) != 32:
            raise ApprovalCryptoError(
                f"{env_name} 解码后长度 {len(key)} 字节，必须为 32 字节"
            )
        return key
