package loop_controller.interaction.delegation

import rego.v1

default decision := {
    "verdict": "deny",
    "reason": "no interaction policy allows this delegation",
    "policy_hits": ["default_deny"],
}

capability := input.source_profile.capabilities[input.tool_name]

base_allowed if {
    input.action_kind == "delegation"
    capability.allowed == true
    input.trust.trust_level in {"limited", "full"}
    "delegate_execution" in input.target_capabilities
}

argument_denied if {
    some name, denied_values in capability.denied_args
    input.arguments[name] in denied_values
}

argument_not_allowed if {
    some name, allowed_values in capability.allowed_args
    count(allowed_values) > 0
    not input.arguments[name] in allowed_values
}

approval_required if {
    capability.require_approval == true
}

approval_required if {
    input.risk_level == "critical"
}

approval_required if {
    input.target_profile == null
    input.source_profile.require_approval_for_external == true
}

arguments_rejected if {
    argument_denied
}

arguments_rejected if {
    argument_not_allowed
}

decision := {
    "verdict": "deny",
    "reason": "delegation arguments denied by interaction policy",
    "policy_hits": ["argument_policy"],
} if {
    base_allowed
    arguments_rejected
}

decision := {
    "verdict": "require_approval",
    "reason": "delegation requires owner approval",
    "policy_hits": ["approval_required"],
} if {
    base_allowed
    not argument_denied
    not argument_not_allowed
    approval_required
}

decision := {
    "verdict": "allow",
    "reason": "delegation allowed by interaction policy",
    "policy_hits": ["delegation_allowed"],
} if {
    base_allowed
    not argument_denied
    not argument_not_allowed
    not approval_required
}
