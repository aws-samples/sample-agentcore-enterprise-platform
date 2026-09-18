"""{{name}} — {{summary}}

Scaffolded by `deploy.sh usecase new {{name}}`. It deploys and verifies as-is:
one SSM parameter under this use case's namespace, carrying the gateway URL it
discovered through the platform interface. Replace the body of the stack with
what your use case actually needs; keep the two rules below.

The rules (docs/PLATFORM_INTERFACE.md):
- Consume the platform through its SSM parameter namespace, resolved AT DEPLOY
  TIME (value_for_string_parameter renders a CloudFormation dynamic reference),
  never by importing a core stack.
- Publish your own outputs under {ssm_prefix}/use-cases/{{name}}/ so other tools
  can discover you the same way you discovered the platform.
"""

import aws_cdk as cdk
from aws_cdk import aws_ssm as ssm
from constructs import Construct


class UseCaseStack(cdk.Stack):
    def __init__(self, scope: Construct, id: str, *, ctx: dict, config: dict, **kw):
        super().__init__(scope, id, **kw)
        # Platform interface, deploy-time resolution: synthesizes without the
        # platform deployed, resolves against the real value on deploy.
        gateway_url = ssm.StringParameter.value_for_string_parameter(
            self, f"{ctx['ssm_prefix']}/gateway/url"
        )
        # `config` is your raw block from platform.yaml (`use_cases: {{name}}: {...}`).
        label = config.get("label", "{{name}}")
        ssm.StringParameter(
            self,
            "GatewaySeen",
            parameter_name=f"{ctx['ssm_prefix']}/use-cases/{{name}}/gateway-seen",
            string_value=f"{label}: {gateway_url}",
        )
        cdk.CfnOutput(self, "Label", value=label)


def build(app, ctx: dict, config: dict) -> None:
    """Entry point the platform calls for each enabled use case."""
    UseCaseStack(
        app,
        f"{ctx['prefix']}-uc-{{name}}",
        ctx=ctx,
        config=config,
        env=ctx["cdk_env"],
    )
