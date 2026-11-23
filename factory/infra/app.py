"""Software factory CDK app. Separate from the repo's app.py so existing stacks synth unchanged.

From a workstation (still supported, and the fallback when CI is unavailable):

    cd factory/infra && ./preflight.sh -c coordinator_role=<role>
    cdk diff --all -c coordinator_role=<role>
    cdk deploy --all -c coordinator_role=<role>

Deploys run from a workstation only; nothing outside the account is trusted to deploy.
"""

import os
import sys

import aws_cdk as cdk

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)  # repo root: infra_utils

from cicd_stack import SoftwareFactoryCicdStack
from factory_stacks import SoftwareFactoryDataStack, SoftwareFactoryStack

app = cdk.App()
env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
)
# Deploy with the caller's own credentials instead of assuming the CDK bootstrap roles. The
# bootstrap cfn-exec role carries AdministratorAccess, so the bootstrap path would give the
# GitHub pipeline an administrator route into the account. The workflow instead passes
# `--role-arn software-factory-cdk-exec`, a role scoped to the factory's own resources. A
# workstation deploy is unaffected: it already uses the operator's credentials.
# A synthesizer binds to one stack, so each stack gets its own instance.
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
    synthesizer=cdk.CliCredentialsStackSynthesizer(),
    termination_protection=True,
    description="Software factory: run records, webhook dedupe, secrets",
)
SoftwareFactoryStack(
    app,
    "SoftwareFactory",
    data=data,
    coordinator_role_name=coordinator_role,
    env=env,
    synthesizer=cdk.CliCredentialsStackSynthesizer(),
    description="Software factory: AgentCore runtime, GitHub webhook ingress, budget",
)
SoftwareFactoryCicdStack(
    app,
    "SoftwareFactoryCicd",
    env=env,
    synthesizer=cdk.CliCredentialsStackSynthesizer(),
    description="Software factory: the CloudFormation execution role",
)
cdk.Tags.of(app).add("Project", "SoftwareFactory")
app.synth()
