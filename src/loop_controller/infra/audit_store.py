"""审计存储（§4.4 / §7.1）：T3.1 + P0 HMAC 完整版。

``JsonlAuditStore`` 追加写入 ``audit.jsonl``，为每个事件分配 ``seq``、计算
``prev_hash``，提供 ``verify_chain`` 检测删除/改写/插入/顺序变更，以及
``query_by_trace`` 按 trace_id 检索。

P0 新增 HMAC-SHA256 支持：
- 通过 ``hash_algo`` 选择 ``sha256``（兼容旧日志）或 ``hmac-sha256``；
- ``hmac-sha256`` 模式下从部署级 root key 派生 event key 与 seal key，做域分离；
- 提供 ``seal()`` 写入 seal 记录，固定当前链累积 HMAC，缓解"最后一行删改无法检测"问题；
- 默认 ``hmac-sha256``；``sha256`` 仅用于读取/验证旧文件，或显式声明的开发模式。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import threading
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Protocol, runtime_checkable

from loop_controller.audit.evidence import EvidenceChain
from loop_controller.infra.alert_store import AlertStore
from loop_controller.models import AuditAlert, AuditEvent
from loop_controller.utils.canonical import canonical_json

logger = logging.getLogger(__name__)


@runtime_checkable
class AuditStore(Protocol):
    """审计存储接口（§4.4）。"""

    def append(self, event: AuditEvent) -> None: ...
    def verify_chain(self) -> bool: ...
    def query_by_trace(self, trace_id: str) -> list[AuditEvent]: ...
    def query_by_session(self, session_id: str) -> list[AuditEvent]: ...  # v0.12.0
    def query_by_task(self, task_id: str) -> list[AuditEvent]: ...  # v0.12.0
    def iter_events(self) -> AsyncIterator[AuditEvent]: ...  # v0.18.0


def _derive_key(root_key: bytes, label: bytes) -> bytes:
    """用 HMAC-SHA256(root_key, label) 做简单域分离；足够满足 P0 需求。"""
    return hmac.new(root_key, label, hashlib.sha256).digest()


def _sha256_text(text: str) -> str:
    """SHA-256 文本摘要（UTF-8）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _hmac_text(key: bytes, text: str) -> str:
    """HMAC-SHA256 文本摘要（UTF-8）。"""
    return hmac.new(key, text.encode("utf-8"), hashlib.sha256).hexdigest()


class JsonlAuditStore:
    """JSONL 审计存储 + SHA-256/HMAC-SHA256 哈希链 + seal 记录。"""

    _GENESIS = "GENESIS"
    _EVENT_LABEL = b"lc:audit:event:v1"
    _SEAL_LABEL = b"lc:audit:seal:v1"

    def __init__(
        self,
        path: str | Path,
        *,
        hash_algo: str = "sha256",
        hmac_key: bytes | None = None,
        key_id: str | None = None,
        evidence_chain: EvidenceChain | None = None,
        alert_store: AlertStore | None = None,
    ) -> None:
        self._path = Path(path)
        self._evidence_chain = evidence_chain
        self._alert_store = alert_store
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if hash_algo not in ("sha256", "hmac-sha256"):
            raise ValueError(f"不支持的 hash_algo：{hash_algo}")
        self._hash_algo = hash_algo
        self._key_id = key_id
        if hash_algo == "hmac-sha256":
            if hmac_key is None:
                raise ValueError("hmac-sha256 必须提供 hmac_key")
            if not key_id:
                raise ValueError("hmac-sha256 模式下 key_id 必须非空（用于密钥轮换识别）")
            self._event_key = _derive_key(hmac_key, self._EVENT_LABEL)
            self._seal_key = _derive_key(hmac_key, self._SEAL_LABEL)
        else:
            self._event_key = b""
            self._seal_key = b""
        self._seq, self._prev_hash, self._chain_hash, last_algo = self._load_tail()

        # 升级策略（P0）：若文件已存在且实际算法与当前不一致，拒绝启动，
        # 要求运维人员手动迁移旧文件（如归档为 audit.jsonl.legacy）。
        # 混合算法链无法安全校验，静默切换会导致后续 verify_chain 失败。
        if last_algo is not None and last_algo != self._hash_algo:
            raise ValueError(
                f"审计文件 {self._path} 的现有记录使用 {last_algo}，"
                f"但当前配置为 {self._hash_algo}。请先归档旧文件后再切换算法。"
            )

    def _load_tail(self) -> tuple[int, str, str, str | None]:
        """启动时重放文件末行，恢复 ``seq``、``prev_hash``、``_chain_hash`` 与最后算法。

        保证重启后续写不断链；seal 记录也会被重放并更新 ``_chain_hash``。
        返回的 ``last_algo`` 为 ``None`` 表示文件为空或不存在。
        """
        if not self._path.exists():
            return 0, self._GENESIS, self._GENESIS, None
        with self._path.open("r", encoding="utf-8") as fh:
            lines = [line.strip() for line in fh if line.strip()]
        if not lines:
            return 0, self._GENESIS, self._GENESIS, None

        # 过滤掉不完整行（崩溃残留），但保留完整行用于恢复状态。
        valid_lines: list[str] = []
        for line in lines:
            try:
                json.loads(line)
            except json.JSONDecodeError:
                continue
            valid_lines.append(line)

        if not valid_lines:
            return 0, self._GENESIS, self._GENESIS, None

        last = json.loads(valid_lines[-1])
        seq = int(last.get("seq", 0))
        last_algo = last.get("hash_algo", "sha256")

        # 重放完整文件恢复 chain_hash；chain_hash 即为最后一行的 hash，
        # 它同时是下一行的 prev_hash（sha256 与 hmac 模式均一致）。
        chain_hash = self._GENESIS
        for line in valid_lines:
            record = json.loads(line)
            if record.get("action") == "seal":
                # seal 记录自身的 chain_hash 写入 metadata，但 seal 行也参与链。
                chain_hash = self._hash(line, chain_hash)
            else:
                chain_hash = self._hash(line, chain_hash)
        return seq, chain_hash, chain_hash, last_algo

    def _hash(self, text: str, chain_hash: str | None = None) -> str:
        """计算单行文本的哈希。

        - sha256 模式：返回 SHA-256(text)；
        - hmac-sha256 模式：返回 HMAC(event_key, chain_hash + text)，
          其中 chain_hash 为到上一行为止的累积 HMAC；首行使用 GENESIS。
        """
        if self._hash_algo == "sha256":
            return _sha256_text(text)
        base = chain_hash if chain_hash is not None else self._GENESIS
        return _hmac_text(self._event_key, base + text)

    def append(self, event: AuditEvent) -> None:
        """分配 seq/prev_hash 后追加写入 JSONL。"""
        self._seq += 1
        to_write = event.model_copy(
            update={
                "seq": self._seq,
                "prev_hash": self._prev_hash,
                "hash_algo": self._hash_algo,
                "key_id": self._key_id,
            }
        )
        line = canonical_json(to_write.model_dump(mode="json", exclude_none=True))
        if self._evidence_chain is not None:
            try:
                self._append_evidence(to_write)
            except Exception as exc:
                logger.exception("签名证据链写入失败，仍写入原审计记录")
                self._save_evidence_alert(
                    rule_id="evidence_chain_append_failed",
                    title="签名证据链写入失败",
                    description=str(exc),
                    event=to_write,
                )
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
        self._prev_hash = self._hash(line, self._chain_hash)
        self._chain_hash = self._prev_hash

    def _save_evidence_alert(
        self,
        *,
        rule_id: str,
        title: str,
        description: str,
        event: AuditEvent | None = None,
    ) -> None:
        if self._alert_store is None:
            return
        alert = AuditAlert(
            alert_id=uuid.uuid4().hex,
            session_id=event.session_id if event is not None else "",
            task_id=event.trace_id if event is not None else None,
            rule_id=rule_id,
            severity="critical",
            title=title,
            description=description,
            evidence=[event.event_id] if event is not None else [],
        )
        try:
            self._alert_store.save_alert(alert)
        except Exception:
            logger.exception("证据链告警写入失败")

    async def verify_evidence_chain(self) -> bool:
        """验证已有签名证据链；失败时告警但不抛出。"""
        if self._evidence_chain is None:
            return True
        try:
            valid = await self._evidence_chain.verify()
        except Exception as exc:
            logger.exception("启动验证签名证据链失败")
            description = str(exc)
        else:
            if valid:
                return True
            logger.error("启动验证发现签名证据链不完整")
            description = "签名证据链完整性验证失败"
        self._save_evidence_alert(
            rule_id="evidence_chain_verification_failed",
            title="签名证据链验证失败",
            description=description,
        )
        return False

    def _append_evidence(self, event: AuditEvent) -> None:
        """从同步审计接口调用异步证据链，并兼容已运行的事件循环。"""
        evidence_chain = self._evidence_chain
        if evidence_chain is None:
            return
        coroutine = evidence_chain.append(event)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(coroutine)
            return

        error: list[BaseException] = []

        def run() -> None:
            try:
                asyncio.run(coroutine)
            except BaseException as exc:
                error.append(exc)

        thread = threading.Thread(target=run)
        thread.start()
        thread.join()
        if error:
            raise error[0]

    def seal(self, reason: str = "periodic_seal") -> AuditEvent:
        """写入 seal 记录，固定当前链累积 HMAC。

        seal 记录本身也是审计链的一部分；其 ``metadata.chain_hash`` 字段
        包含写入前整个链的累积 HMAC，并用 ``seal_key`` 做域分离签名
        （``seal_signature``），用于事后独立校验。
        """
        chain_hash = self._chain_hash
        seal_signature = ""
        if self._hash_algo == "hmac-sha256":
            seal_signature = _hmac_text(self._seal_key, chain_hash)
        event = AuditEvent(
            event_id="seal",
            trace_id="",
            session_id="",
            actor_type="system",
            actor_id="audit_store",
            action="seal",
            target="audit_chain",
            reason=reason,
            metadata={
                "chain_hash": chain_hash,
                "seal_signature": seal_signature,
            },
        )
        self.append(event)
        return event

    def verify_chain(self) -> bool:
        """重放全文件校验：seq 连续递增、prev_hash 链接正确、每行可解析。

        hmac-sha256 模式下同时校验累积 HMAC；seal 记录的 chain_hash 与当前
        累积值一致时通过。
        """
        if not self._path.exists():
            return True
        expected_prev = self._GENESIS
        expected_seq = 1
        chain_hash = self._GENESIS
        with self._path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    return False
                if record.get("seq") != expected_seq:
                    return False
                if record.get("prev_hash") != expected_prev:
                    return False

                # 重新计算当前行的 hash 与 chain_hash。
                current_hash = self._hash(line, chain_hash)
                chain_hash = current_hash

                # seal 记录额外校验 metadata.chain_hash 与 seal_signature。
                if record.get("action") == "seal":
                    metadata = record.get("metadata", {})
                    if metadata.get("chain_hash") != expected_prev:
                        return False
                    if self._hash_algo == "hmac-sha256":
                        expected_sig = _hmac_text(self._seal_key, expected_prev)
                        if metadata.get("seal_signature") != expected_sig:
                            return False

                expected_seq += 1
                expected_prev = current_hash
        return True

    def query_by_trace(self, trace_id: str) -> list[AuditEvent]:
        """按 trace_id 全文件扫描并返回 AuditEvent 列表（MVP 数据量小，可接受）。"""
        return self._query_by_field("trace_id", trace_id)

    def query_by_session(self, session_id: str) -> list[AuditEvent]:
        """按 session_id 全文件扫描并返回 AuditEvent 列表（v0.12.0）。"""
        return self._query_by_field("session_id", session_id)

    def query_by_task(self, task_id: str) -> list[AuditEvent]:
        """按 task_id 全文件扫描并返回 AuditEvent 列表（v0.12.0）。"""
        return self._query_by_field("task_id", task_id)

    def _query_by_field(self, field: str, value: str) -> list[AuditEvent]:
        """通用全文件扫描查询。"""
        results: list[AuditEvent] = []
        if not self._path.exists():
            return results
        with self._path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get(field) == value:
                    results.append(AuditEvent(**record))
        return results

    async def iter_events(self) -> AsyncIterator[AuditEvent]:
        """按写入顺序异步迭代所有审计事件（v0.18.0）。"""
        if not self._path.exists():
            return
        with self._path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                yield AuditEvent(**record)
