r"""研究助手端到端示例（§9.2 A1 / 迭代 1 里程碑演示）.

执行步骤：
1. 启动 OPA：``opa run --server --addr 127.0.0.1:8181 policies/``
2. 运行本脚本：
   ``$env:PYTHONPATH="src"; .venv\Scripts\python.exe examples/research_agent_example.py``

预期结果（迭代 1）：
- web_search → allow → 返回 Mock 搜索结果
- read_file  → allow → 读取 /data/kb/ai_compliance_checklist.md
- write_file → allow → 写入 /data/output/summary.md
- send_email → require_approval，但迭代 1 未接通审批，按 deny 处理并结束任务

所有动作都会写入 ``data/audit.jsonl``。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

# 让脚本在未安装包时也能运行
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from loop_controller.infra.config_loader import ConfigLoader
from loop_controller.models import Agent, Task
from loop_controller.runtime import build_runtime, run_task


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

    _ensure_demo_paths()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    # 加载并校验配置（ConfigLoader 会访问 OPA 做启动校验）
    loader = ConfigLoader()
    config = loader.load(CONFIG_DIR, opa_base_url=OPA_URL)

    agent = config.agents["researcher_001"]
    task = Task(
        task_id="research-demo-001",
        session_id="research-demo-001",
        user_id="alice",
        agent_id=agent.agent_id,
        description="调研 AI 合规问题并发送摘要邮件给张经理",
    )

    runtime = build_runtime(config, opa_url=OPA_URL)
    await runtime.start()
    try:
        await run_task(task, agent, runtime)
        logger = logging.getLogger(__name__)
        logger.info("任务执行完成，审计日志：%s", config.audit_log_path)
    finally:
        await runtime.aclose()


if __name__ == "__main__":
    asyncio.run(main())
