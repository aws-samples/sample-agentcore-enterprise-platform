# ═══════════════════════════════════════════════════════════════
# AgentCore Gateway configuration guardrails (control-plane SCPs)
#
# Uses the launched AgentCore Gateway configuration condition keys to constrain what admins
# can Create/Update. These are caller-side (IAM) control-plane controls. Data-plane ingress
# controls (aws:SourceVpc / InboundJwtClaim) and RCP support launch later — see
# docs/SECURITY_CONTROLS.md.
#
# All files are read from the shared control-library; scalar <<sentinels>> are injected with
# replace() (never templatefile(), so IAM policy variables are untouched).
# ═══════════════════════════════════════════════════════════════

locals {
  gw_dir = "${path.module}/${var.control_library_path}/scp/gateway"

  gateway_scps = {
    "require-cmk" = replace(
      file("${local.gw_dir}/require-cmk.json"),
      "<<kms_key_arn_pattern>>", var.gateway_kms_key_arn_pattern
    )
    "deny-no-auth"          = file("${local.gw_dir}/deny-no-auth.json")
    "require-policy-engine" = file("${local.gw_dir}/require-policy-engine.json")
    "enforce-approved-idp" = replace(
      file("${local.gw_dir}/enforce-approved-idp.json"),
      "<<approved_discovery_url_pattern>>", var.approved_discovery_url_pattern
    )
    "restrict-protocol" = replace(
      file("${local.gw_dir}/restrict-protocol.json"),
      "<<allowed_protocol>>", var.allowed_gateway_protocol
    )
    "targets-require-private-endpoint"     = file("${local.gw_dir}/targets-require-private-endpoint.json")
    "targets-restrict-credential-provider" = file("${local.gw_dir}/targets-restrict-credential-provider.json")
    "targets-restrict-type"                = file("${local.gw_dir}/targets-restrict-type.json")
  }

  gateway_scps_active = var.enable_gateway_scps ? local.gateway_scps : {}

  gateway_attachments = var.enable_gateway_scps ? {
    for pair in setproduct(keys(local.gateway_scps), var.target_ids) :
    "${pair[0]}::${pair[1]}" => { policy_key = pair[0], target_id = pair[1] }
  } : {}
}

resource "aws_organizations_policy" "gateway" {
  for_each    = local.gateway_scps_active
  name        = "${var.name_prefix}-scp-gateway-${each.key}"
  description = "AgentCore Gateway hardening SCP: ${each.key}"
  type        = "SERVICE_CONTROL_POLICY"
  content     = each.value
}

resource "aws_organizations_policy_attachment" "gateway" {
  for_each  = local.gateway_attachments
  policy_id = aws_organizations_policy.gateway[each.value.policy_key].id
  target_id = each.value.target_id
}
