// Permit only the read-only sample tool. Write/mutating tools stay denied by
// 00-default-forbid.cedar (Cedar has no explicit allow-override of a forbid, so denies win —
// this permit only takes effect for actions NOT covered by a forbid; keep forbids narrow to
// specific write actions in production, or rely on default-forbid + explicit read permits).
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
