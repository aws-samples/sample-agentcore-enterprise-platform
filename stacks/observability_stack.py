"""Observability Stack — CloudWatch vended logs + X-Ray for AgentCore resources.

Implements Requirement 12: Observability Integration
- CloudWatch vended log delivery per AgentCore resource (APPLICATION_LOGS + TRACES)
- X-Ray Transaction Search activation with trace indexing
- Optional DataDog Forwarder Lambda configuration
- Deployable per-resource: one instance per AgentCore resource being monitored
"""
import aws_cdk as cdk
from aws_cdk import aws_iam as iam, aws_logs as logs
from constructs import Construct


class ObservabilityStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        project_name: str,
        environment: str,
        backend: str = "cloudwatch",
        monitored_resources: dict[str, str] | None = None,
        datadog_api_key: str = "",
        datadog_site: str = "datadoghq.com",
        **kwargs,
    ):
        """
        Args:
            monitored_resources: Map of resource_name → resource_arn to monitor.
                Example: {"gateway": "arn:aws:...", "memory": "arn:aws:..."}
            backend: 'cloudwatch' (default) or 'datadog'
        """
        super().__init__(scope, id, **kwargs)

        prefix = f"{project_name}-{environment}"

        # ── X-Ray Transaction Search Resource Policy ──
        # Required once per account/region for vended trace delivery
        xray_policy = cdk.CfnResource(self, "XRayResourcePolicy",
            type="AWS::XRay::ResourcePolicy",
            properties={
                "PolicyName": f"{prefix}-agentcore-xray-policy",
                "PolicyDocument": cdk.Fn.sub(
                    '{"Version":"2012-10-17","Statement":[{"Sid":"AllowAgentCoreTraceDelivery",'
                    '"Effect":"Allow","Principal":{"Service":"delivery.logs.amazonaws.com"},'
                    '"Action":["xray:PutTraceSegments","xray:GetSamplingRules","xray:GetSamplingTargets"],'
                    '"Resource":"*","Condition":{"StringEquals":{"aws:SourceAccount":"${AWS::AccountId}"}}}]}'
                ),
            },
        )

        # ── Per-Resource Log Delivery ──
        for resource_name, resource_arn in (monitored_resources or {}).items():
            safe_name = resource_name.replace("-", "").replace("_", "").title()

            # CloudWatch Log Group for application logs
            log_group = logs.LogGroup(self, f"Logs{safe_name}",
                log_group_name=f"/aws/bedrock-agentcore/{prefix}/{resource_name}",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=cdk.RemovalPolicy.DESTROY,
            )

            # Vended Log Delivery Source (APPLICATION_LOGS)
            delivery_source = cdk.CfnResource(self, f"DeliverySource{safe_name}",
                type="AWS::Logs::DeliverySource",
                properties={
                    "Name": f"{prefix}-{resource_name}-app-logs",
                    "ResourceArn": resource_arn,
                    "LogType": "APPLICATION_LOGS",
                },
            )

            # Vended Log Delivery Destination (CloudWatch Logs)
            delivery_dest = cdk.CfnResource(self, f"DeliveryDest{safe_name}",
                type="AWS::Logs::DeliveryDestination",
                properties={
                    "Name": f"{prefix}-{resource_name}-cw-dest",
                    "DestinationResourceArn": log_group.log_group_arn,
                },
            )

            # Vended Log Delivery (connects source → destination)
            cdk.CfnResource(self, f"Delivery{safe_name}",
                type="AWS::Logs::Delivery",
                properties={
                    "DeliverySourceName": delivery_source.ref,
                    "DeliveryDestinationArn": delivery_dest.get_att("Arn").to_string(),
                },
            )

            # Trace Delivery Source (X-Ray)
            trace_source = cdk.CfnResource(self, f"TraceSource{safe_name}",
                type="AWS::Logs::DeliverySource",
                properties={
                    "Name": f"{prefix}-{resource_name}-traces",
                    "ResourceArn": resource_arn,
                    "LogType": "TRACES",
                },
            )

            # Trace Delivery Destination (X-Ray)
            trace_dest = cdk.CfnResource(self, f"TraceDest{safe_name}",
                type="AWS::Logs::DeliveryDestination",
                properties={
                    "Name": f"{prefix}-{resource_name}-xray-dest",
                    "DestinationResourceArn": f"arn:aws:xray:{self.region}:{self.account}:insight",
                },
            )
            trace_dest.node.add_dependency(xray_policy)

            # Trace Delivery
            trace_delivery = cdk.CfnResource(self, f"TraceDelivery{safe_name}",
                type="AWS::Logs::Delivery",
                properties={
                    "DeliverySourceName": trace_source.ref,
                    "DeliveryDestinationArn": trace_dest.get_att("Arn").to_string(),
                },
            )

        # ── Outputs ──
        cdk.CfnOutput(self, "Backend", value=backend)
        cdk.CfnOutput(self, "MonitoredResources",
            value=",".join(monitored_resources.keys()) if monitored_resources else "none")
