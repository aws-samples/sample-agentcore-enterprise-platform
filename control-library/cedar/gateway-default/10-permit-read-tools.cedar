// Permit only the read-only sample tool. Cedar is default-deny: anything not explicitly
// permitted is denied, so no blanket forbid policy is needed (or wanted — a matching forbid
// always overrides every permit, which would make this permit dead code). This permit allows
// only the listed read tools; add more permit policies for additional tools. If a forbid is
// ever added, remember it overrides permits unconditionally — keep forbids narrow.
//
// AgentCore action name format:  AgentCore::Action::"<TargetName>___<tool_name>"
// (three underscores between the gateway target name and the tool name).
// Resource is the gateway:        AgentCore::Gateway::"<gateway-arn>"
//
// To extend: add each read/low-risk tool's action to the list below. Do NOT add write,
// update, delete, or payment tools here — leaving them out keeps them denied by default.
permit(
  principal,
  action in [AgentCore::Action::"<<read_action>>"],
  resource
);
