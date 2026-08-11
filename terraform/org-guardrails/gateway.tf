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
#
# QUOTA: AWS Organizations allows at most 5 policies of a type attached to a single
# root/OU/account, and FullAWSAccess already consumes one slot (4 usable). Attaching the 8
# gateway controls as individual SCPs (plus the memory SCP in main.tf) would need 9 slots and
# fail against any real target. So the per-control JSON files stay granular in the
# control-library (customers can consume them individually via AFT/StackSets/console), and
# this module merges their Statement arrays into ONE consolidated gateway SCP at apply time.
# Total with everything enabled: 1 gateway SCP + 1 memory SCP = 2 attachments per target.
# ═══════════════════════════════════════════════════════════════

locals {
  gw_dir = "${path.module}/${var.control_library_path}/scp/gateway"

  # Each control rendered individually from its library file (sentinels injected with
  # replace(), never templatefile()). Kept as a map so per-control provenance is inspectable
  # in `terraform console`.
  gateway_scp_rendered = {
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

  # Concatenate every control's Statement array into one document. keys() iterates in
  # lexicographic order, so statement order is deterministic across plans. Sids are unique
  # across the library files (enforced by the precondition below), so merging is safe.
  gateway_scp_statements = flatten([
    for name in keys(local.gateway_scp_rendered) :
    jsondecode(local.gateway_scp_rendered[name]).Statement
  ])

  gateway_scp_consolidated = jsonencode({
    Version   = "2012-10-17"
    Statement = local.gateway_scp_statements
  })
}

resource "aws_organizations_policy" "gateway" {
  count = var.enable_gateway_scps ? 1 : 0

  name        = "${var.name_prefix}-scp-gateway-guardrails"
  description = "Consolidated AgentCore Gateway hardening SCP (all gateway controls merged to fit the 5-SCPs-per-target quota)."
  type        = "SERVICE_CONTROL_POLICY"
  content     = local.gateway_scp_consolidated

  lifecycle {
    # SCP documents max out at 5,120 characters. Fail at plan time with the actual size so a
    # future control addition that pushes the merged document over the quota is caught before
    # any API call.
    precondition {
      condition     = length(local.gateway_scp_consolidated) < 5120
      error_message = "Consolidated gateway SCP is ${length(local.gateway_scp_consolidated)} characters, which exceeds the 5,120-character SCP document quota. Split the gateway controls into a second consolidated policy (one more attachment slot is available) or trim statements."
    }

    # Merging is only safe if no two library files reuse a Sid.
    precondition {
      condition     = length(distinct(local.gateway_scp_statements[*].Sid)) == length(local.gateway_scp_statements)
      error_message = "Duplicate Sid detected across control-library/scp/gateway/*.json. Every statement must carry a unique Sid before the documents can be merged into one SCP."
    }
  }
}

resource "aws_organizations_policy_attachment" "gateway" {
  for_each = var.enable_gateway_scps ? toset(var.target_ids) : toset([])

  policy_id = aws_organizations_policy.gateway[0].id
  target_id = each.value
}
