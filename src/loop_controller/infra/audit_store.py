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
from concurrent.futures import ThreadPoolExecutor
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
    async def append_async(self, event: AuditEvent) -> None: ...
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
        self._sync_lock = threading.Lock()
        self._write_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="audit-writer")
        self._write_blocked = False
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

    def _prepare_event(self, event: AuditEvent) -> tuple[AuditEvent, str]:
        self._seq += 1
        to_write = event.model_copy(
            update={
                "seq": self._seq,
                "prev_hash": self._prev_hash,
                "hash_algo": self._hash_algo,
                "key_id": self._key_id,
            }
        )
        return to_write, canonical_json(to_write.model_dump(mode="json", exclude_none=True))

    def _write_audit_line(self, line: str) -> None:
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
        self._prev_hash = self._hash(line, self._chain_hash)
        self._chain_hash = self._prev_hash

    def _write_checkpoint(self, evidence_seq: int, evidence_hash: str) -> None:
        if self._evidence_chain is None:
            return
        self._evidence_chain.write_checkpoint(
            {
                "audit_seq": self._seq,
                "audit_hash": self._chain_hash,
                "evidence_seq": evidence_seq,
                "evidence_hash": evidence_hash,
            }
        )

    def _mark_evidence_degraded(self, reason: str) -> None:
        if self._evidence_chain is not None and hasattr(self._evidence_chain, "mark_degraded"):
            self._evidence_chain.mark_degraded(reason)

    def _handle_checkpoint_failure(self, exc: Exception, event: AuditEvent) -> None:
        description = f"evidence checkpoint write failed: {type(exc).__name__}"
        self._mark_evidence_degraded(description)
        logger.error("证据 checkpoint 写入失败，业务和审计已提交：%s", type(exc).__name__)
        self._save_evidence_alert(
            rule_id="evidence_checkpoint_write_failed",
            title="证据 checkpoint 写入失败",
            description=description,
            event=event,
        )

    def _handle_audit_write_failure(self, exc: Exception, event: AuditEvent) -> None:
        description = f"audit write failed after evidence commit: {type(exc).__name__}"
        self._write_blocked = True
        self._mark_evidence_degraded(description)
        logger.error("审计写入失败，证据已提交；后续写入已阻断")
        self._save_evidence_alert(
            rule_id="audit_write_failed_after_evidence_commit",
            title="审计写入失败，证据已提交",
            description=description,
            event=event,
        )

    def _append_locked(self, event: AuditEvent) -> None:
        """追加事件；调用方必须已持有 ``_sync_lock``。"""
        if self._write_blocked:
            raise RuntimeError("审计存储因先前写入失败已阻断；请重建 Store 后恢复")
        to_write, line = self._prepare_event(event)
        evidence = None
        if self._evidence_chain is not None:
            try:
                evidence = asyncio.run(self._evidence_chain.append(to_write))
            except Exception as exc:
                logger.error("签名证据链写入失败，仍写入原审计记录：%s", type(exc).__name__)
                description = f"evidence append failed: {type(exc).__name__}"
                self._mark_evidence_degraded(description)
                self._save_evidence_alert(
                    rule_id="evidence_chain_append_failed",
                    title="签名证据链写入失败",
                    description=description,
                    event=to_write,
                )
        try:
            self._write_audit_line(line)
        except Exception as exc:
            if evidence is not None:
                self._handle_audit_write_failure(exc, to_write)
            raise RuntimeError("审计记录写入失败") from None
        if (
            evidence is not None
            and evidence.seq == to_write.seq
            and self.evidence_status == "healthy"
        ):
            try:
                self._write_checkpoint(evidence.seq, evidence.current_hash)
            except Exception as exc:
                self._handle_checkpoint_failure(exc, to_write)

    def _append_serialized(self, event: AuditEvent) -> None:
        with self._sync_lock:
            self._append_locked(event)

    def append(self, event: AuditEvent) -> None:
        """同步分配序号并依次写入证据、审计和签名尾 checkpoint。"""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError("运行中的事件循环内不得调用 append()；请使用 await append_async()")
        self._append_serialized(event)

    async def append_async(self, event: AuditEvent) -> None:
        """通过专用单线程执行器异步有序写入，不占用事件循环默认线程池。"""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._write_executor, self._append_serialized, event)

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

    def _audit_events(self) -> list[AuditEvent]:
        if not self._path.exists():
            return []
        events: list[AuditEvent] = []
        with self._path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if line:
                    events.append(AuditEvent.model_validate_json(line))
        return events

    def _audit_hash_at_seq(self, target_seq: int) -> str:
        if target_seq == 0:
            return self._GENESIS
        chain_hash = self._GENESIS
        with self._path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                record = json.loads(line)
                chain_hash = self._hash(line, chain_hash)
                if int(record["seq"]) == target_seq:
                    return chain_hash
        raise ValueError("checkpoint 审计序号在历史链中不存在")

    async def _verify_evidence_consistency(self) -> None:
        evidence_chain = self._evidence_chain
        if evidence_chain is None:
            return
        if not self.verify_chain() or not await evidence_chain.verify():
            raise ValueError("审计或签名证据哈希链完整性验证失败")
        audit_events = self._audit_events()
        evidence = [item async for item in evidence_chain._backend.iter_evidence(None)]
        if len(audit_events) != len(evidence):
            raise ValueError(
                f"审计—证据记录数不一致：audit={len(audit_events)}, evidence={len(evidence)}"
            )
        for audit_event, signed in zip(audit_events, evidence, strict=True):
            if audit_event.event_id != signed.event.event_id or audit_event.seq != signed.event.seq:
                raise ValueError(f"审计—证据事件标识或序号不一致：{audit_event.event_id}")
            audit_digest = _sha256_text(canonical_json(audit_event.model_dump(mode="json")))
            evidence_digest = _sha256_text(canonical_json(signed.event.model_dump(mode="json")))
            if not hmac.compare_digest(audit_digest, evidence_digest):
                raise ValueError(f"审计—证据事件摘要不一致：{audit_event.event_id}")

        checkpoint_path = evidence_chain.checkpoint_path()
        checkpoint = evidence_chain.read_checkpoint()
        if checkpoint_path is not None and checkpoint is None and (audit_events or evidence):
            raise ValueError("证据 checkpoint 缺失但审计或证据数据非空")
        if checkpoint is None:
            return
        audit_seq = audit_events[-1].seq if audit_events else 0
        evidence_seq = evidence[-1].seq if evidence else 0
        evidence_hash = evidence[-1].current_hash if evidence else ""
        checkpoint_audit_seq = int(checkpoint["audit_seq"])
        checkpoint_evidence_seq = int(checkpoint["evidence_seq"])
        if checkpoint_audit_seq != checkpoint_evidence_seq:
            raise ValueError("checkpoint 审计与证据序号不一致")
        if audit_seq != evidence_seq:
            raise ValueError("审计与证据尾部序号不一致")
        if audit_seq < checkpoint_audit_seq:
            raise ValueError("审计或证据尾部相对 checkpoint 发生序号回退")
        historical_audit_hash = self._audit_hash_at_seq(checkpoint_audit_seq)
        historical_evidence_hash = (
            "" if checkpoint_evidence_seq == 0 else evidence[checkpoint_evidence_seq - 1].current_hash
        )
        if historical_audit_hash != checkpoint["audit_hash"]:
            raise ValueError("审计历史哈希与 checkpoint 不一致")
        if historical_evidence_hash != checkpoint["evidence_hash"]:
            raise ValueError("证据历史哈希与 checkpoint 不一致")
        if audit_seq > checkpoint_audit_seq:
            evidence_chain.write_checkpoint(
                {
                    "audit_seq": audit_seq,
                    "audit_hash": self._chain_hash,
                    "evidence_seq": evidence_seq,
                    "evidence_hash": evidence_hash,
                }
            )

    async def verify_evidence_chain(self) -> bool:
        """交叉验证审计、签名证据和 checkpoint；失败时降级但不阻塞启动。"""
        if self._evidence_chain is None:
            return True
        try:
            await self._verify_evidence_consistency()
        except Exception as exc:
            logger.error("启动验证签名证据链失败：%s", type(exc).__name__)
            description = f"evidence chain verification failed: {type(exc).__name__}"
            self._write_blocked = True
            if hasattr(self._evidence_chain, "mark_degraded"):
                self._evidence_chain.mark_degraded(description)
        else:
            self._write_blocked = False
            if hasattr(self._evidence_chain, "status"):
                self._evidence_chain.status = "healthy"
                self._evidence_chain.degraded_reason = None
            return True
        self._save_evidence_alert(
            rule_id="evidence_chain_verification_failed",
            title="签名证据链验证失败",
            description=description,
        )
        return False

    @property
    def evidence_status(self) -> str:
        if self._evidence_chain is None:
            return "disabled"
        return getattr(self._evidence_chain, "status", "healthy")

    def seal(self, reason: str = "periodic_seal") -> AuditEvent:
        """写入 seal 记录，固定当前链累积 HMAC。

        seal 记录本身也是审计链的一部分；其 ``metadata.chain_hash`` 字段
        包含写入前整个链的累积 HMAC，并用 ``seal_key`` 做域分离签名
        （``seal_signature``），用于事后独立校验。
        """
        with self._sync_lock:
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
            self._append_locked(event)
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
