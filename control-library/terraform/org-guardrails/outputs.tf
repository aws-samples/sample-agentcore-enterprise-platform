output "memory_enforce_cmk_policy_id" {
  description = "ID of the 'enforce CMK on Memory' SCP (null if disabled)."
  value       = var.enable_scp_memory_enforce_cmk ? aws_organizations_policy.memory_enforce_cmk[0].id : null
}

output "memory_enforce_cmk_policy_arn" {
  description = "ARN of the 'enforce CMK on Memory' SCP (null if disabled)."
  value       = var.enable_scp_memory_enforce_cmk ? aws_organizations_policy.memory_enforce_cmk[0].arn : null
}

output "gateway_scp_policy_id" {
  description = "ID of the consolidated Gateway hardening SCP (null if disabled)."
  value       = var.enable_gateway_scps ? aws_organizations_policy.gateway[0].id : null
}

output "gateway_scp_policy_arn" {
  description = "ARN of the consolidated Gateway hardening SCP (null if disabled)."
  value       = var.enable_gateway_scps ? aws_organizations_policy.gateway[0].arn : null
}

output "identity_deny_token_for_userid_policy_id" {
  description = "ID of the 'deny GetWorkloadAccessTokenForUserId' SCP (null if disabled)."
  value       = var.enable_scp_identity_deny_token_for_userid ? aws_organizations_policy.identity_deny_token_for_userid[0].id : null
}

output "identity_deny_token_for_userid_policy_arn" {
  description = "ARN of the 'deny GetWorkloadAccessTokenForUserId' SCP (null if disabled)."
  value       = var.enable_scp_identity_deny_token_for_userid ? aws_organizations_policy.identity_deny_token_for_userid[0].arn : null
}

output "attached_target_ids" {
  description = "Target IDs that received at least one SCP attachment (empty if all SCPs are disabled)."
  value       = (var.enable_scp_memory_enforce_cmk || var.enable_gateway_scps || var.enable_scp_identity_deny_token_for_userid) ? var.target_ids : []
}

output "attachments_per_target" {
  description = "Number of SCPs this module attaches to each target (memory + consolidated gateway + identity). Must stay <= 4 usable slots (5-per-target quota minus FullAWSAccess)."
  value       = (var.enable_scp_memory_enforce_cmk ? 1 : 0) + (var.enable_gateway_scps ? 1 : 0) + (var.enable_scp_identity_deny_token_for_userid ? 1 : 0)
}
