r"""研究助手端到端示例（v0.3.0 Iteration 5 异步审批演示）.

执行步骤：
1. 启动 OPA：``opa run --server --addr 127.0.0.1:8181 policies/``
2. 运行本脚本：
   ``$env:PYTHONPATH="src"; .venv\Scripts\python.exe examples/research_agent_example.py``

预期结果：
- web_search → allow → 返回 Mock 搜索结果
- read_file  → allow → 读取 /data/kb/ai_compliance_checklist.md
- write_file → allow → 写入 /data/output/summary.md
- send_email → require_approval → 脚本自动模拟审批通过并继续执行

生产环境应使用 ``lc approvals approve <decision_id>`` 由真实审批人审批。
所有动作都会写入 ``data/audit.jsonl``。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# 让脚本在未安装包时也能运行
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from loop_controller.infra.config_loader import ConfigLoader
from loop_controller.models import Agent, ApprovalRecord
from loop_controller.runtime import build_runtime, resume_task, run_task


CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = PROJECT_ROOT / "logs"
OPA_URL = os.environ.get("OPA_URL", "http://127.0.0.1:8181")


def _ensure_demo_paths() -> None:
    """方案配置文件使用 /data/kb 与 /data/output；在 Windows 上映射为当前盘根目录。"""
    kb = Path("/data/kb")
    out = Path("/data/output")
    kb.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    checklist = kb / "ai_compliance_checklist.md"
    if not checklist.exists():
        checklist.write_text(
            "# AI 合规 checklist\n\n- 数据隐私\n- 模型安全\n- 可解释性\n",
            encoding="utf-8",
        )


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger = logging.getLogger(__name__)

    _ensure_demo_paths()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    # 加载并校验配置（ConfigLoader 会访问 OPA 做启动校验）
    loader = ConfigLoader()
    config = loader.load(CONFIG_DIR, opa_base_url=OPA_URL)

    agent = config.agents["researcher_001"]

    runtime = build_runtime(config, opa_url=OPA_URL)
    task, _session = runtime.create_task(
        user_id="alice",
        agent_id=agent.agent_id,
        description="调研 AI 合规问题并发送摘要邮件给张经理",
    )
    await runtime.start()
    try:
        result = await run_task(task, agent, runtime)

        if result.status == "needs_approval":
            logger.info(
                "send_email 需要人工审批。decision_id=%s request_id=%s",
                result.decision_id,
                result.request_id,
            )
            logger.info("生产环境请执行：lc approvals approve %s --approver zhang_manager", result.decision_id)
            # 演示：自动模拟审批通过（仅用于本地演示）
            assert result.decision_id is not None and result.request_id is not None
            runtime.approval_manager._store.record_response(
                ApprovalRecord(
                    request_id=result.request_id,
                    decision_id=result.decision_id,
                    verdict="approve",
                    approver_id="zhang_manager",
                    comment="auto-approved by demo",
                    decided_at=datetime.now(timezone.utc),
                )
            )
            logger.info("演示模式：自动审批通过，继续执行...")
            await resume_task(task, agent, runtime, pending=result)

        logger.info("任务执行完成，审计日志：%s", config.audit_log_path)
    finally:
        await runtime.aclose()


if __name__ == "__main__":
    asyncio.run(main())
