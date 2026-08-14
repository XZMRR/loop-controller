# Loop Controller — 研究助手场景默认策略
#
# 运行方式：
#   opa run --server --bundle policies/
# 测试方式：
#   POST http://127.0.0.1:8181/v1/data/loop_controller/tool_permission
#   {"input": {"proposal": {...}, "profile": {...}, "task": {...}}}

package loop_controller.tool_permission

import rego.v1

# 默认拒绝（兜底）
default result := {"verdict": "deny", "reason": "Policy default deny"}

# 工具必须在 CapabilityProfile 的 allowed_tools 列表中
tool_allowed if {
	some t in input.profile.allowed_tools
	t == input.proposal.tool_name
}

# 是否为内部邮箱
is_internal_email(email) if endswith(lower(email), "@company.com")

# 是否为允许的文件路径（MVP 阶段限定在 /tmp/ 或 /allowed/）
allowed_path(path) if startswith(path, "/tmp/")
allowed_path(path) if startswith(path, "/allowed/")

# 拒绝原因集合
denial_reasons contains msg if {
	not tool_allowed
	msg := sprintf("Tool %s not in allowed_tools", [input.proposal.tool_name])
}

denial_reasons contains msg if {
	input.proposal.tool_name == "write_file"
	not allowed_path(input.proposal.arguments.path)
	msg := sprintf("Write path %s not allowed", [input.proposal.arguments.path])
}

denial_reasons contains msg if {
	input.proposal.tool_name == "read_file"
	not allowed_path(input.proposal.arguments.path)
	msg := sprintf("Read path %s not allowed", [input.proposal.arguments.path])
}

# 需要人工审批的原因集合
approval_reasons contains msg if {
	input.proposal.tool_name == "send_email"
	not is_internal_email(input.proposal.arguments.to)
	msg := sprintf("External email address %s requires R0-delegate approval", [input.proposal.arguments.to])
}

# 高风险的组合动作：读取文件后发送外部邮件（MVP 静态规则）
approval_reasons contains "Read file then send external email may leak data" if {
	input.proposal.tool_name == "send_email"
	not is_internal_email(input.proposal.arguments.to)
	input.task.description != ""
}

# 决策规则：先检查拒绝，再检查审批，最后允许
result := {"verdict": "deny", "reason": concat("; ", denial_reasons)} if {
	count(denial_reasons) > 0
}

result := {"verdict": "require_approval", "reason": concat("; ", approval_reasons)} if {
	count(denial_reasons) == 0
	count(approval_reasons) > 0
}

result := {"verdict": "allow", "reason": "Policy allows this action"} if {
	count(denial_reasons) == 0
	count(approval_reasons) == 0
}
