output "memory_enforce_cmk_policy_id" {
  description = "ID of the 'enforce CMK on Memory' SCP (null if disabled)."
  value       = var.enable_scp_memory_enforce_cmk ? aws_organizations_policy.memory_enforce_cmk[0].id : null
}

output "memory_enforce_cmk_policy_arn" {
  description = "ARN of the 'enforce CMK on Memory' SCP (null if disabled)."
  value       = var.enable_scp_memory_enforce_cmk ? aws_organizations_policy.memory_enforce_cmk[0].arn : null
}

output "attached_target_ids" {
  description = "Target IDs the SCPs were attached to."
  value       = var.enable_scp_memory_enforce_cmk ? var.target_ids : []
}
