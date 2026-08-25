"""hello-platform — the reference use case.

Deploys one zero-cost resource that proves the whole extension mechanism:
an SSM parameter recording the gateway URL this use case discovered through
the platform interface. If this deploys and verify.py passes, the contract
between the platform and its use cases works end to end.

The rules this file demonstrates (docs/PLATFORM_INTERFACE.md):

- Consume the platform via its SSM parameter namespace, resolved AT DEPLOY
  TIME (value_for_string_parameter renders a CloudFormation dynamic
  parameter) — never import a core stack.
- Publish your own outputs under {ssm_prefix}/use-cases/<name>/ so other
  tools can discover you the same way you discovered the platform.
- build() is the only required symbol. It gets the app, a small context, and
  your raw config block from platform.yaml.
"""

import aws_cdk as cdk
from aws_cdk import aws_ssm as ssm
from constructs import Construct


class HelloPlatformStack(cdk.Stack):
    def __init__(self, scope: Construct, id: str, *, ctx: dict, config: dict, **kw):
        super().__init__(scope, id, **kw)

        # Platform interface, deploy-time resolution: this synthesizes without
        # the platform deployed, and resolves against the real value on deploy.
        gateway_url = ssm.StringParameter.value_for_string_parameter(
            self, f"{ctx['ssm_prefix']}/gateway/url"
        )

        greeting = config.get("greeting", "hello")

        ssm.StringParameter(
            self,
            "Seen",
            parameter_name=f"{ctx['ssm_prefix']}/use-cases/hello-platform/gateway-seen",
            string_value=f"{greeting}: {gateway_url}",
        )
        cdk.CfnOutput(self, "Greeting", value=greeting)


def build(app, ctx: dict, config: dict) -> None:
    """Entry point the platform calls for each enabled use case."""
    HelloPlatformStack(
        app,
        f"{ctx['prefix']}-uc-hello-platform",
        ctx=ctx,
        config=config,
        env=ctx["cdk_env"],
    )
