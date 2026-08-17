"""Masker 分级掩码与超长截断测试（T3.2 / A13）。"""

from __future__ import annotations

import hashlib

from loop_controller.infra.config_loader import MaskingRules, ValuePattern
from loop_controller.masker import Masker


def _rules() -> MaskingRules:
    return MaskingRules(
        field_name_blacklist=["password", "api_key"],
        value_patterns=[
            ValuePattern(name="email", pattern=r"[\w.+-]+@[\w-]+\.[\w.]+", replacement="***@***"),
            ValuePattern(name="bearer", pattern=r"Bearer\s+\S+", replacement="***"),
        ],
        masking_applies_to={
            "audit_log": ["field_name_blacklist", "value_patterns"],
            "approval_request": ["field_name_blacklist"],
        },
    )


def test_audit_log_masks_both_field_and_value() -> None:
    masker = Masker(_rules())
    args = {
        "to": "zhang@company.com",
        "password": "secret123",
        "content": "Hello",
    }
    masked = masker.mask(args, "audit_log")
    assert masked["password"] == "***"
    assert masked["to"] == "***@***"
    assert masked["content"] == "Hello"


def test_approval_request_masks_only_credentials() -> None:
    masker = Masker(_rules())
    args = {
        "to": "zhang@company.com",
        "password": "secret123",
        "content": "Hello",
    }
    masked = masker.mask(args, "approval_request")
    # 审批视图必须能看到真实收件人与正文，否则审批形同虚设
    assert masked["to"] == "zhang@company.com"
    assert masked["password"] == "***"
    assert masked["content"] == "Hello"


def test_long_value_truncated() -> None:
    masker = Masker(_rules())
    long_text = "x" * 600
    args = {"content": long_text}
    masked = masker.mask(args, "audit_log")
    truncated = masked["content"]
    assert isinstance(truncated, dict)
    assert truncated["length"] == 600
    assert truncated["preview"] == "x" * 100
    assert truncated["sha256"] == hashlib.sha256(long_text.encode("utf-8")).hexdigest()


def test_nested_args() -> None:
    masker = Masker(_rules())
    args = {
        "credentials": {"password": "p", "api_key": "k"},
        "list": ["a@b.com", "plain"],
    }
    masked = masker.mask(args, "audit_log")
    assert masked["credentials"]["password"] == "***"
    assert masked["credentials"]["api_key"] == "***"
    assert masked["list"] == ["***@***", "plain"]


def test_no_mutation() -> None:
    masker = Masker(_rules())
    args = {"password": "secret"}
    masker.mask(args, "audit_log")
    assert args["password"] == "secret"
