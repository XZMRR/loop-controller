"""LoopController 最小验证脚本（v0.13.0）。

Agent 自己掌握主循环，每次调工具前交给 LoopController 治理。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from loop_controller.controller import build_controller
from loop_controller.infra.config_loader import ConfigLoader
from loop_controller.models import ApprovalRecord

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
OPA_URL = os.environ.get("OPA_URL", "http://127.0.0.1:8181")


async def main() -> None:
    config = ConfigLoader().load(CONFIG_DIR, opa_base_url=OPA_URL)
    controller = await build_controller(
        config,
        opa_url=OPA_URL,
        env_extra={"PYTHONPATH": str(PROJECT_ROOT / "src")},
    )
    await controller.start()

    try:
        # 1. 低风险 web_search → allow
        result = await controller.evaluate_and_execute(
            agent_id="researcher_001",
            user_id="alice",
            tool_name="web_search",
            arguments={"query": "AI compliance"},
            task_context="搜索公开资料",
        )
        print(f"[web_search] status={result.status} content={result.content!r}")
        assert result.status == "allow"

        # 2. 高风险 send_email → require_approval
        result = await controller.evaluate_and_execute(
            agent_id="researcher_001",
            user_id="alice",
            tool_name="send_email",
            arguments={
                "to": "zhang@company.com",
                "subject": "AI 合规摘要",
                "body": "请查收",
            },
            task_context="发送邮件",
        )
        print(f"[send_email] status={result.status} request_id={result.request_id}")
        assert result.status == "require_approval"

        # 模拟 CLI 审批通过
        store = controller._runtime.approval_manager._store
        request = store.get_request(result.decision.decision_id)
        assert request is not None
        store.record_response(
            ApprovalRecord(
                request_id=request.request_id,
                decision_id=request.decision_id,
                verdict="approve",
                approver_id="zhang_manager",
                comment="approved in demo",
            )
        )

        # 4. resume 执行
        final = await controller.resume_after_approval(result.request_id)
        print(f"[resume] status={final.status} content={final.content!r}")
        assert final.status == "allow"

        print("LoopController 接口验证通过")
    finally:
        await controller.aclose()


if __name__ == "__main__":
    asyncio.run(main())
