package loop_controller.tool_permission

import rego.v1

# 默认拒绝
default decision := {"verdict": "deny", "reason": "no policy allows this action", "policy_hits": ["default_deny"]}

# ---- 通用规则：critical 风险信号必须人工审批（分类器信号经由制度生效）----
decision := {"verdict": "require_approval", "reason": "critical risk signal requires approval",
             "escalation_target": input.agent.owner_id, "policy_hits": ["critical_signal_gate"]} if {
    input.risk_level == "critical"
}

# ---- v0.10.0 能力组合风险门控：A+B>C ----
# input.action.combination_risk_tags 不存在时规则自然失败，兼容旧 input。

decision := {"verdict": "deny", "reason": "detected data exfil pattern: read + external email",
             "policy_hits": ["capability_data_exfil_deny"]} if {
    some tag in input.action.combination_risk_tags
    tag == "data_exfil"
    count(input.action.authority_token_ids) == 0
}

# v0.11.0：持有有效 AuthorityToken 的 data_exfil 从 deny 降级为 require_approval
decision := {"verdict": "require_approval", "reason": "data exfil with authority token requires final approval",
             "escalation_target": input.agent.owner_id, "policy_hits": ["capability_data_exfil_token_approval"]} if {
    some tag in input.action.combination_risk_tags
    tag == "data_exfil"
    count(input.action.authority_token_ids) > 0
}

decision := {"verdict": "require_approval", "reason": "detected data upload pattern: read + external http",
             "escalation_target": input.agent.owner_id, "policy_hits": ["capability_data_exfil_http_approval"]} if {
    some tag in input.action.combination_risk_tags
    tag == "data_exfil_http"
}

# ---- 会话风险门控：异常累积 → 一律升级人工审批 ----
# input.session_risk 不存在时规则自然失败，不会崩溃（兼容旧日志/测试）
decision := {"verdict": "require_approval", "reason": "session risk score above threshold",
             "escalation_target": input.agent.owner_id, "policy_hits": ["session_risk_gate"]} if {
    session_risk_above_threshold
    input.risk_level != "critical"   # critical 门控更严，避免重复命中
}

# 辅助规则：会话风险分超过阈值；不存在 session_risk 时失败，即不触发
session_risk_above_threshold if {
    input.session_risk.score >= input.session_risk.threshold
}

# ---- local_functions：集成测试用简单本地函数 ----
decision := {"verdict": "allow", "reason": "local function add allowed", "policy_hits": ["add_allow"]} if {
    input.tool_name == "add"
    input.risk_level != "critical"
    not session_risk_above_threshold
}

decision := {"verdict": "allow", "reason": "local function echo allowed", "policy_hits": ["echo_allow"]} if {
    input.tool_name == "echo"
    input.risk_level != "critical"
    not session_risk_above_threshold
}

decision := {"verdict": "allow", "reason": "local function raise_error allowed", "policy_hits": ["raise_error_allow"]} if {
    input.tool_name == "raise_error"
    input.risk_level != "critical"
    not session_risk_above_threshold
}

decision := {"verdict": "allow", "reason": "local function hang_forever allowed", "policy_hits": ["hang_forever_allow"]} if {
    input.tool_name == "hang_forever"
    input.risk_level != "critical"
    not session_risk_above_threshold
}

# ---- web_search ----
decision := {"verdict": "allow", "reason": "web search allowed", "policy_hits": ["web_search_allow"]} if {
    input.tool_name == "web_search"
    input.risk_level != "critical"
    not session_risk_above_threshold
}

# ---- fetch_url：允许访问公开 HTTP 资源 ----
decision := {"verdict": "allow", "reason": "fetch_url allowed", "policy_hits": ["fetch_url_allow"]} if {
    input.tool_name == "fetch_url"
    input.risk_level != "critical"
    not session_risk_above_threshold
}

# ---- read_file：限目录 ----
decision := {"verdict": "allow", "reason": "read within allowed directories", "policy_hits": ["read_file_allow"]} if {
    input.tool_name == "read_file"
    input.risk_level != "critical"
    not session_risk_above_threshold
    some pattern in input.profile.tools.read_file.allowed_args.path
    glob.match(pattern, ["/"], input.arguments.path)
}

# ---- list_directory：限目录 ----
decision := {"verdict": "allow", "reason": "list within allowed directories", "policy_hits": ["list_directory_allow"]} if {
    input.tool_name == "list_directory"
    input.risk_level != "critical"
    not session_risk_above_threshold
    some pattern in input.profile.tools.list_directory.allowed_args.path
    glob.match(pattern, ["/"], input.arguments.path)
}

# ---- write_file：限目录 ----
decision := {"verdict": "allow", "reason": "write within allowed directories", "policy_hits": ["write_file_allow"]} if {
    input.tool_name == "write_file"
    input.risk_level != "critical"
    not session_risk_above_threshold
    some pattern in input.profile.tools.write_file.allowed_args.path
    glob.match(pattern, ["/"], input.arguments.path)
}

# ---- query_database：只允许 SELECT ----
decision := {"verdict": "allow", "reason": "read-only database query allowed", "policy_hits": ["query_database_allow"]} if {
    input.tool_name == "query_database"
    input.risk_level != "critical"
    not session_risk_above_threshold
    startswith(upper(trim_space(input.arguments.sql)), "SELECT")
}

decision := {"verdict": "deny", "reason": "query_database only supports SELECT", "policy_hits": ["query_database_deny_non_select"]} if {
    input.tool_name == "query_database"
    not startswith(upper(trim_space(input.arguments.sql)), "SELECT")
}

# ---- update_database：涉及写数据，强制审批 ----
decision := {"verdict": "require_approval", "reason": "update_database requires human approval",
             "escalation_target": input.agent.owner_id, "policy_hits": ["update_database_approval"]} if {
    input.tool_name == "update_database"
    input.risk_level != "critical"
}

# ---- send_email：白名单内收件人 → 按 Profile 决定是否审批；白名单外 → deny ----
decision := {"verdict": "require_approval", "reason": "send_email requires human approval",
             "escalation_target": input.agent.owner_id, "policy_hits": ["send_email_approval"]} if {
    input.tool_name == "send_email"
    input.risk_level != "critical"
    input.profile.tools.send_email.require_approval == true
    recipient_allowed
}

decision := {"verdict": "allow", "reason": "internal email allowed", "policy_hits": ["send_email_allow"]} if {
    input.tool_name == "send_email"
    input.risk_level != "critical"
    input.profile.tools.send_email.require_approval == false
    recipient_allowed
    not session_risk_above_threshold
}

decision := {"verdict": "deny", "reason": "recipient outside allowed patterns", "policy_hits": ["send_email_deny_external"]} if {
    input.tool_name == "send_email"
    not recipient_allowed
}

recipient_allowed if {
    some pattern in input.profile.tools.send_email.allowed_args.to
    glob.match(pattern, [], lower(input.arguments.to))
}
