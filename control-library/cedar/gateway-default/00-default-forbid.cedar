// Default-deny baseline for AgentCore Gateway tool calls.
// Every tool invocation is forbidden unless an explicit permit policy (evaluated together)
// allows it. This is the "default forbid on writes" foundation: with only this policy in
// place, no tool can be called; you then add narrow permits for the read/low-risk tools.
forbid(principal, action, resource);
