"""Observability Stack — CloudWatch vended logs for AgentCore resources.

Implements Requirement 12: Observability Integration
- CloudWatch vended log delivery per AgentCore resource (APPLICATION_LOGS)
- Log groups with 1-month retention
"""
import aws_cdk as cdk
from aws_cdk import aws_logs as logs
from constructs import Construct


class ObservabilityStack(cdk.Stack):
    def __init__(self, scope: Construct, id: str, *, project_name: str, environment: str,
                 backend: str = "cloudwatch", monitored_resources: dict[str, str] | None = None,
                 **kwargs):
        super().__init__(scope, id, **kwargs)

        prefix = f"{project_name}-{environment}"

        for resource_name, resource_arn in (monitored_resources or {}).items():
            safe_name = resource_name.replace("-", "").replace("_", "").title()

            log_group = logs.LogGroup(self, f"Logs{safe_name}",
                log_group_name=f"/aws/bedrock-agentcore/{prefix}/{resource_name}",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=cdk.RemovalPolicy.DESTROY,
            )

            # Vended log delivery source
            source = cdk.CfnResource(self, f"Source{safe_name}",
                type="AWS::Logs::DeliverySource",
                properties={
                    "Name": f"{prefix}-{resource_name}-app-logs",
                    "ResourceArn": resource_arn,
                    "LogType": "APPLICATION_LOGS",
                },
            )

            # Vended log delivery destination
            dest = cdk.CfnResource(self, f"Dest{safe_name}",
                type="AWS::Logs::DeliveryDestination",
                properties={
                    "Name": f"{prefix}-{resource_name}-cw-dest",
                    "DestinationResourceArn": log_group.log_group_arn,
                },
            )

            # Connect source → destination
            cdk.CfnResource(self, f"Delivery{safe_name}",
                type="AWS::Logs::Delivery",
                properties={
                    "DeliverySourceName": source.ref,
                    "DeliveryDestinationArn": dest.get_att("Arn").to_string(),
                },
            )

        cdk.CfnOutput(self, "Backend", value=backend)
        cdk.CfnOutput(self, "MonitoredResources",
            value=",".join(monitored_resources.keys()) if monitored_resources else "none")
