"""sqlite MCP server 集成测试（v0.14.0）。

验证 query_database / update_database 在真实 sqlite3 后端 + R2 治理下的行为。
不启动 filesystem（避免 npx），只使用 Python sqlite + email_mock server。
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from loop_controller.models import ApprovalRecord
from tests.controller_helpers import controller_for

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    """构造仅含 sqlite/email_mock 的最小真实配置。"""
    root = tmp_path / "project"
    root.mkdir()
    shutil.copytree(REPO_ROOT / "config", root / "config")
    shutil.copytree(REPO_ROOT / "policies", root / "policies")
    (root / "data").mkdir()

    db_path = root / "data" / "company.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS customers ("
        "id INTEGER PRIMARY KEY, name TEXT, email TEXT, region TEXT)"
    )
    conn.execute("DELETE FROM customers")
    conn.executemany(
        "INSERT INTO customers (name, email, region) VALUES (?, ?, ?)",
        [
            ("Alice", "alice@company.com", "cn"),
            ("Bob", "bob@company.com", "cn"),
        ],
    )
    conn.commit()
    conn.close()

    (root / "config" / "mcp_servers.yaml").write_text(
        """
servers:
  sqlite:
    command: ["python", "-m", "loop_controller.mcp_servers.sqlite_server", "data/company.db"]
    transport: stdio
  email_mock:
    command: ["python", "-m", "loop_controller.mocks.email_server"]
    transport: stdio

tool_mapping:
  query_database: {server: sqlite, mcp_name: query, cost_per_call: 300}
  update_database: {server: sqlite, mcp_name: execute, cost_per_call: 800}
""",
        encoding="utf-8",
    )
    (root / "config" / "profiles.yaml").write_text(
        """
profiles:
  - profile_id: research_assistant_v1
    description: sqlite 验证 profile
    max_budget_token: 100000
    max_budget_payment: 0.0
    tools:
      query_database:
        allowed: true
        allowed_args:
          sql: ["SELECT*"]
        max_calls_per_task: 10
      update_database:
        allowed: true
        require_approval: true
        allowed_args:
          sql: ["INSERT*", "UPDATE*"]
        max_calls_per_task: 1
""",
        encoding="utf-8",
    )
    return root


@pytest.mark.asyncio
async def test_sqlite_select_and_update_requires_approval(
    workdir: Path,
    opa_server: str,
) -> None:
    """SELECT 直接返回，INSERT 触发 require_approval，审批后真实写入数据库。"""
    os.environ["LOOP_CONTROLLER_AUDIT_HMAC_KEY"] = "a" * 64
    controller = await controller_for(workdir, opa_server)

    try:
        select_result = await controller.evaluate_and_execute(
            agent_id="researcher_001",
            user_id="alice",
            tool_name="query_database",
            arguments={"sql": "SELECT * FROM customers"},
            task_context="sqlite e2e",
        )
        assert select_result.status == "allow"

        insert_result = await controller.evaluate_and_execute(
            agent_id="researcher_001",
            user_id="alice",
            tool_name="update_database",
            arguments={
                "sql": "INSERT INTO customers (name, email, region) VALUES ('Carol', 'carol@company.com', 'cn')"
            },
            task_context="sqlite e2e",
        )
        assert insert_result.status == "require_approval"
        assert insert_result.decision is not None

        store = controller._runtime.approval_manager._store
        request = store.get_request(insert_result.decision.decision_id)
        store.record_response(
            ApprovalRecord(
                request_id=request.request_id,
                decision_id=insert_result.decision.decision_id,
                verdict="approve",
                approver_id="zhang_manager",
                comment="approved",
            )
        )

        final = await controller.resume_after_approval(insert_result.request_id)
        assert final.status == "allow"
    finally:
        await controller.aclose()

    # 验证数据库真的写入了 Carol
    conn = sqlite3.connect(str(workdir / "data" / "company.db"))
    try:
        rows = conn.execute("SELECT name FROM customers").fetchall()
        names = {row[0] for row in rows}
        assert "Carol" in names
    finally:
        conn.close()
