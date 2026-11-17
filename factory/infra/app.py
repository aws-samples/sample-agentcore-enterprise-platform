"""Software factory CDK app. Separate from the repo's app.py so existing stacks synth unchanged.

cd factory/infra && cdk diff -c coordinator_role=<role> && cdk deploy --all -c coordinator_role=<role>
"""

import os
import sys

import aws_cdk as cdk

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)  # repo root: infra_utils

from factory_stacks import SoftwareFactoryDataStack, SoftwareFactoryStack

app = cdk.App()
env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
)
coordinator_role = app.node.try_get_context(
    "coordinator_role"
)  # IAM role name the coordinator assumes from
if not coordinator_role:
    raise SystemExit(
        "pass -c coordinator_role=<IAM role name in this account> (trusted by software-factory-readonly)"
    )
data = SoftwareFactoryDataStack(
    app,
    "SoftwareFactoryData",
    env=env,
    termination_protection=True,
    description="Software factory: run records, webhook dedupe, secrets",
)
SoftwareFactoryStack(
    app,
    "SoftwareFactory",
    data=data,
    coordinator_role_name=coordinator_role,
    env=env,
    description="Software factory: AgentCore runtime, GitHub webhook ingress, budget",
)
cdk.Tags.of(app).add("Project", "SoftwareFactory")
app.synth()
