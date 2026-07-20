variable "name_prefix" {
  description = "Prefix for created SCP names."
  type        = string
  default     = "agentcore"
}

variable "control_library_path" {
  description = <<-EOT
    Path to the control-library/ root, relative to this module. Both this Terraform module
    and the CDK app read the same raw policy files from there (single source of truth).
  EOT
  type        = string
  default     = "../../control-library"
}

variable "target_ids" {
  description = <<-EOT
    Organizations root, OU, or account IDs to attach the SCPs to. SCPs are additive-deny and
    apply on top of the existing FullAWSAccess policy, so attaching is non-destructive.
    Example: ["ou-abcd-1234wxyz"] or ["r-abcd"].
  EOT
  type        = list(string)
}

# ── Control: scp.memory.enforce-cmk ──
variable "enable_scp_memory_enforce_cmk" {
  description = "Create and attach the 'deny CreateMemory without a CMK' SCP."
  type        = bool
  default     = true
}

variable "kms_key_arn_pattern" {
  description = <<-EOT
    KMS key ARN pattern that AgentCore CreateMemory must reference. Default requires any CMK.
    Narrow to a specific key/OU-owned key ARN pattern to restrict further. Injected over the
    <<kms_key_arn_pattern>> sentinel in control-library/scp/memory/enforce-cmk.json.
  EOT
  type        = string
  default     = "arn:aws:kms:*:*:key/*"
}

# ── Controls: Gateway configuration SCPs (control plane) ──
variable "enable_gateway_scps" {
  description = "Create and attach the AgentCore Gateway configuration hardening SCPs."
  type        = bool
  default     = true
}

variable "gateway_kms_key_arn_pattern" {
  description = "KMS key ARN pattern a Gateway must use (bedrock-agentcore:KmsKeyArn)."
  type        = string
  default     = "arn:aws:kms:*:*:key/*"
}

variable "approved_discovery_url_pattern" {
  description = <<-EOT
    Allowed OIDC discovery URL pattern for JWT gateways (bedrock-agentcore:DiscoveryUrl).
    Default only requires HTTPS; narrow to your IdP, e.g.
    "https://login.microsoftonline.com/<tenant>/*".
  EOT
  type        = string
  default     = "https://*"
}

variable "allowed_gateway_protocol" {
  description = "Allowed Gateway protocolType (bedrock-agentcore:ProtocolType)."
  type        = string
  default     = "MCP"
}
