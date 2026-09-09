<!-- markdownlint-disable MD041 -->

# Networking module

The `networking` stack (`stacks/networking_stack.py`) is the platform's
optional VPC: private subnets for the AgentCore runtime network interfaces,
interface endpoints for the AWS services the agents call, and a
least-privilege security group. Enabling it does two things at once — it
creates the VPC, *and* `app.py` places every runtime in it. This is not an
air-gapped network: the private subnets keep a NAT route because agents call
AWS APIs and some patterns use the public internet.

## When it deploys

| Path | Setting |
|---|---|
| `platform.yaml` | `security.networking: true` |
| Env var / CDK context | `ENABLE_NETWORKING=true` / `-c enable_networking=true` |
| Guided workshop | Module C |
| Preset | `security-focused` and `platform-team` enable it |

Off by default.

## What it creates

| Resource | Details |
|---|---|
| VPC | `10.0.0.0/16`, max 2 AZs, 1 NAT gateway |
| Subnets | One public + one private (`PRIVATE_WITH_EGRESS`) `/24` per AZ |
| Runtime security group | No inbound rules; egress limited to TCP 443 (every dependency — Bedrock, AgentCore, Secrets Manager, ECR, CloudWatch Logs — is HTTPS) |
| Interface endpoints | Bedrock Runtime, ECR (API + Docker), CloudWatch Logs, AgentCore Gateway (`com.amazonaws.<region>.bedrock-agentcore.gateway`) |
| Gateway endpoint | S3 (free; ECR image layers live in S3) |
| Endpoint policy | On the AgentCore endpoint, org-scoped via `control-library/vpce/agentcore-in-org.json` — only when `org_id` is set |
| SSM parameters | `/<project>/<env>/networking/{vpc-id,private-subnet-ids,runtime-security-group-id}` |
| Outputs | `VpcId`, `PrivateSubnetIds`, `RuntimeSecurityGroupId` |

## Configuration

| Key (platform.yaml) | Env var | Default | Effect |
|---|---|---|---|
| `security.networking` | `ENABLE_NETWORKING` | `false` | Creates the stack and puts runtimes in VPC mode |
| `security.org_id` | `ORG_ID` | *(empty)* | Renders the org-scoped endpoint policy; **without it the AgentCore endpoint is created with no policy at all** |

The CIDR (`10.0.0.0/16`) and `enable_vpc_endpoints=True` are constructor
parameters that `app.py` does not expose as flags — changing them means
editing `app.py`.

## How runtimes opt in

With the stack enabled, `app.py` builds a `runtime_network` dict — `VPC` mode,
the private subnet IDs, and the runtime security group — and passes it to
every `RuntimeStack` (orchestrator and both A2A agents), each of which also
takes a stack dependency on `networking`. The property shape is produced by
`infra_utils/runtime_network.py::build_network_config`, which exists to
prevent two historic defects:

- **The wrong property shape.** `CfnRuntime` wants
  `networkModeConfig: {subnets, securityGroups}`; an invented key survives
  synth and is ignored or rejected by CloudFormation.
- **Silent fallback to public.** Asking for VPC mode without subnets used to
  render `{"networkMode": "VPC"}` and nothing else — a "network isolated"
  deployment whose agents all ran on the public network. The helper raises
  `ValueError` instead.

Without the stack, runtimes deploy with `networkMode: PUBLIC` (the AgentCore
data plane still authenticates every call; "public" is the egress path, not
an open inbound endpoint).

## Interfaces

- The SSM parameters are the published interface: `scripts/check_network.py`
  and the module C verification read them instead of parsing CloudFormation
  outputs. Within a single synth, `app.py` wires the stack attributes
  directly.

## Security notes

- The runtime security group is the choke point for agent egress: HTTPS only,
  no inbound. Callers reach agents through the AgentCore data plane, never
  through the VPC.
- The endpoint policy constrains **SigV4 callers only**. OAuth/JWT callers
  carry no IAM principal and pass via `Principal: "*"` — a VPC endpoint
  policy cannot constrain them (see item 2 in
  [Security controls](../SECURITY_CONTROLS.md)).
- Private subnets are the only valid placement: a public subnet gives
  AgentCore ENIs no internet route, so the runtimes would lose Bedrock access.
- To remove the NAT you must first add endpoints for *every* service your
  agents call; the shipped endpoint set is not exhaustive for all patterns.

## Verification

```bash
python scripts/check_network.py                  # expect VPC mode
python scripts/check_network.py --expect-public  # deployments without networking
```

Two live checks: (1) the private subnets sit in Availability Zones where
AgentCore can actually place interfaces, and (2) every deployed runtime
reports `networkMode: VPC` in those subnets — the check that would have
caught the original "VPC built, agents still public" defect.
`scripts/verify.py` includes it automatically whenever `networking` is in the
deployment contract, and module C runs it as its verification.

## Gotchas

- **Hourly cost.** The NAT gateway and each interface endpoint bill per hour
  whether or not agents run. The S3 gateway endpoint is free — without it,
  ECR layer pulls would bill as NAT data processing.
- **AZ IDs, not AZ names.** AgentCore supports VPC connectivity only in
  specific AZ *IDs* per region (three per region — see `SUPPORTED_ZONE_IDS`
  in `infra_utils/runtime_network.py`), and an AZ *name* like `us-east-1a`
  maps to a different AZ *ID* in every account. A VPC that works in one
  account can fail in the next, with an opaque error at runtime creation,
  modules later. `check_network.py` catches it early; fix by pinning the VPC
  to supported zones (`aws ec2 describe-availability-zones` shows the
  mapping). An unknown region returns no restriction — a stale table should
  not block a region AgentCore has since added.
- **ENIs linger after leaving VPC mode.** AgentCore `agentic_ai` network
  interfaces can persist for up to ~8 hours after a runtime stops using VPC
  mode, so destroying this stack can fail on the subnets and security group.
  The NAT gateway and endpoints (the hourly charges) delete in the same run;
  retry the destroy after the interfaces age out:
  `aws ec2 describe-network-interfaces --filters Name=interface-type,Values=agentic_ai`.
- **`org_id` is silent.** Unlike the memory resource policy (which hard-fails
  without it), forgetting `org_id` here ships the AgentCore endpoint with no
  policy — no error, no warning from CDK.
