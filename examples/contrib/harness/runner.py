"""Docker 容器内的工具执行器（配合 docker_harness.py）。

读取 /input.json，执行对应工具，把结果以 JSON 打印到 stdout。
当前示例仅实现 shell echo/ls/pwd 和 sqlite 查询，用于演示隔离执行。
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path


def main() -> None:
    input_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/input.json")
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    tool = payload["tool"]
    args = payload.get("args", {})

    if tool == "shell":
        command = args.get("command", "")
        cmd_args = [str(a) for a in args.get("args", [])]
        allowed = {"echo": ["echo"], "ls": ["ls"], "pwd": ["pwd"]}
        base = allowed.get(command)
        if base is None:
            print(json.dumps({"status": "error", "error": "command not allowed"}))
            sys.exit(1)
        proc = subprocess.run(
            base + cmd_args,
            capture_output=True,
            text=True,
            check=False,
        )
        print(json.dumps({
            "status": "success" if proc.returncode == 0 else "error",
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }))
        sys.exit(proc.returncode)

    if tool == "sql":
        sql = args.get("sql", "")
        db = args.get("db", ":memory:")
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        rows = [dict(row) for row in conn.execute(sql).fetchall()]
        print(json.dumps({"status": "success", "rows": rows}))
        sys.exit(0)

    print(json.dumps({"status": "error", "error": f"unknown tool {tool!r}"}))
    sys.exit(1)


if __name__ == "__main__":
    main()
