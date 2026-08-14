"""验证 OPA 策略是否正确加载并可被 OPAPolicyEngine 调用.

运行前需先启动 OPA：
    .\\scripts\\run_opa.ps1
"""

from __future__ import annotations

from loop_controller import OPAPolicyEngine


def main() -> None:
    engine = OPAPolicyEngine()

    test_cases = [
        (
            "read_file allowed",
            {
                "proposal": {"tool_name": "read_file", "arguments": {"path": "/tmp/report.md"}, "task_context": "read", "risk_level": "low"},
                "profile": {"allowed_tools": ["read_file"], "denied_args": {}, "tool_permissions": {}},
                "task": {"task_id": "t1", "user_id": "u1", "session_id": "s1", "description": "test"},
            },
            "allow",
        ),
        (
            "read_file path not allowed",
            {
                "proposal": {"tool_name": "read_file", "arguments": {"path": "/etc/passwd"}, "task_context": "read", "risk_level": "low"},
                "profile": {"allowed_tools": ["read_file"], "denied_args": {}, "tool_permissions": {}},
                "task": {"task_id": "t1", "user_id": "u1", "session_id": "s1", "description": "test"},
            },
            "deny",
        ),
        (
            "send_email internal allowed",
            {
                "proposal": {"tool_name": "send_email", "arguments": {"to": "zhang@company.com"}, "task_context": "send", "risk_level": "high"},
                "profile": {"allowed_tools": ["send_email"], "denied_args": {}, "tool_permissions": {}},
                "task": {"task_id": "t1", "user_id": "u1", "session_id": "s1", "description": "test"},
            },
            "allow",
        ),
        (
            "send_email external requires approval",
            {
                "proposal": {"tool_name": "send_email", "arguments": {"to": "external@gmail.com"}, "task_context": "send", "risk_level": "high"},
                "profile": {"allowed_tools": ["send_email"], "denied_args": {}, "tool_permissions": {}},
                "task": {"task_id": "t1", "user_id": "u1", "session_id": "s1", "description": "test"},
            },
            "require_approval",
        ),
        (
            "write_file unallowed path denied",
            {
                "proposal": {"tool_name": "write_file", "arguments": {"path": "/etc/passwd", "content": "x"}, "task_context": "write", "risk_level": "medium"},
                "profile": {"allowed_tools": ["write_file"], "denied_args": {}, "tool_permissions": {}},
                "task": {"task_id": "t1", "user_id": "u1", "session_id": "s1", "description": "test"},
            },
            "deny",
        ),
    ]

    all_pass = True
    for name, input_doc, expected in test_cases:
        result = engine.evaluate("loop_controller.tool_permission", input_doc)
        verdict = result.get("verdict")
        status = "PASS" if verdict == expected else "FAIL"
        if verdict != expected:
            all_pass = False
        print(f"[{status}] {name}: verdict={verdict}, expected={expected}, reason={result.get('reason')}")

    if all_pass:
        print("\nAll OPA policy tests passed.")
    else:
        print("\nSome OPA policy tests failed.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
