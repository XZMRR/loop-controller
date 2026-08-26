"""SQL 执行器测试（v0.24.0）。"""

from __future__ import annotations

import sqlite3

import pytest

from loop_controller.executors import ExecutionContext, SQLExecutor
from loop_controller.executors.sql_models import DataSourceConfig, SQLToolSpec
from loop_controller.models import CapabilityProfile


def _fake_context() -> ExecutionContext:
    return ExecutionContext(
        call_id="c1",
        task_id="t1",
        agent_id="a1",
        user_id="u1",
    )


@pytest.fixture
def executor(tmp_path) -> SQLExecutor:
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO users (name) VALUES ('alice'), ('bob')")
    conn.commit()
    conn.close()

    data_sources = {
        "test_db": DataSourceConfig(
            name="test_db",
            driver="sqlite",
            database=str(db_path),
        ),
    }
    specs = {
        "query_users": SQLToolSpec(
            tool_name="query_users",
            data_source="test_db",
            read_only=True,
            parameterize=True,
        ),
        "write_users": SQLToolSpec(
            tool_name="write_users",
            data_source="test_db",
            read_only=False,
            parameterize=True,
        ),
    }
    return SQLExecutor(specs, data_sources)


@pytest.mark.asyncio
async def test_sql_select_success(executor: SQLExecutor) -> None:
    result = await executor.execute(
        "query_users",
        {"sql": "SELECT * FROM users WHERE name = 'alice'"},
        _fake_context(),
    )
    assert result.status == "success"
    assert len(result.content) == 1
    assert result.content[0]["name"] == "alice"


@pytest.mark.asyncio
async def test_sql_parameterized_query(executor: SQLExecutor) -> None:
    result = await executor.execute(
        "query_users",
        {
            "sql": "SELECT * FROM users WHERE name = :name",
            "parameters": {"name": "bob"},
        },
        _fake_context(),
    )
    assert result.status == "success"
    assert len(result.content) == 1
    assert result.content[0]["name"] == "bob"


@pytest.mark.asyncio
async def test_sql_read_only_blocks_write(executor: SQLExecutor) -> None:
    result = await executor.execute(
        "query_users",
        {"sql": "INSERT INTO users (name) VALUES ('mallory')"},
        _fake_context(),
    )
    assert result.status == "error"
    assert result.error_code == "sql_read_only_violation"


@pytest.mark.asyncio
async def test_sql_injection_semicolon_blocked(executor: SQLExecutor) -> None:
    result = await executor.execute(
        "query_users",
        {"sql": "SELECT * FROM users; DROP TABLE users"},
        _fake_context(),
    )
    assert result.status == "error"
    assert result.error_code == "sql_injection_blocked"


@pytest.mark.asyncio
async def test_sql_injection_comment_blocked(executor: SQLExecutor) -> None:
    result = await executor.execute(
        "query_users",
        {"sql": "SELECT * FROM users -- OR 1=1"},
        _fake_context(),
    )
    assert result.status == "error"
    assert result.error_code == "sql_injection_blocked"


@pytest.mark.asyncio
async def test_sql_write_allowed_when_not_read_only(executor: SQLExecutor) -> None:
    result = await executor.execute(
        "write_users",
        {"sql": "INSERT INTO users (name) VALUES ('mallory')"},
        _fake_context(),
    )
    assert result.status == "success"


@pytest.mark.asyncio
async def test_sql_unknown_tool() -> None:
    exec_empty = SQLExecutor({}, {})
    result = await exec_empty.execute(
        "unknown",
        {"sql": "SELECT 1"},
        _fake_context(),
    )
    assert result.status == "error"
    assert result.error_code == "sql_tool_not_found"


@pytest.mark.asyncio
async def test_sql_unknown_data_source() -> None:
    specs = {
        "bad_tool": SQLToolSpec(
            tool_name="bad_tool",
            data_source="missing_db",
            read_only=True,
        ),
    }
    exec_bad = SQLExecutor(specs, {})
    result = await exec_bad.execute(
        "bad_tool",
        {"sql": "SELECT 1"},
        _fake_context(),
    )
    assert result.status == "error"
    assert result.error_code == "sql_data_source_not_found"


@pytest.mark.asyncio
async def test_sql_missing_sql_argument(executor: SQLExecutor) -> None:
    result = await executor.execute("query_users", {}, _fake_context())
    assert result.status == "error"
    assert result.error_code == "sql_arg_not_allowed"


@pytest.mark.asyncio
async def test_sql_list_tools_filtered_by_profile(executor: SQLExecutor) -> None:
    profile = CapabilityProfile(
        profile_id="p1",
        tools={"query_users": {"tool_name": "query_users", "allowed": True}},
    )
    tools = await executor.list_tools(profile)
    assert len(tools) == 1
    assert tools[0].canonical_name == "query_users"
