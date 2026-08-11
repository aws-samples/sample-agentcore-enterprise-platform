"""Observability Stack — CloudWatch vended logs and traces for AgentCore resources.

Implements Requirement 12: Observability Integration
- CloudWatch vended log delivery per AgentCore resource (APPLICATION_LOGS)
- Log groups with 1-month retention
- CloudWatch Transaction Search, without which X-Ray rejects every span
"""

import json

import aws_cdk as cdk
from aws_cdk import (
    aws_events as events,
)
from aws_cdk import (
    aws_events_targets as targets,
)
from aws_cdk import (
    aws_iam as iam,
)
from aws_cdk import (
    aws_logs as logs,
)
from aws_cdk import (
    aws_sns as sns,
)
from aws_cdk import (
    custom_resources as cr,
)
from constructs import Construct

from infra_utils.transaction_search import (
    TRACE_DESTINATION,
    transaction_search_caller_statements,
    xray_logs_resource_policy,
)


class ObservabilityStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        project_name: str,
        environment: str,
        monitored_resources: dict[str, str] | None = None,
        enable_traceability: bool = False,
        enable_transaction_search: bool = True,
        **kwargs,
    ):
        super().__init__(scope, id, **kwargs)

        prefix = f"{project_name}-{environment}"

        # ── CloudWatch Transaction Search (prerequisite for any tracing) ──
        # The runtimes emit OTLP spans whether or not this is configured; with
        # the account still pointed at the XRay destination, X-Ray answers every
        # batch with HTTP 400 and the traces this module promises never exist.
        #
        # CAVEAT: both resources below are account- and region-scoped, not
        # per-stack. In an account where a platform team owns tracing centrally,
        # deploy with -c enable_transaction_search=false. Deleting this stack
        # deliberately does NOT revert the destination: other workloads in the
        # account may depend on it by then.
        if enable_transaction_search:
            logs.CfnResourcePolicy(
                self,
                "XRaySpansResourcePolicy",
                policy_name=f"{prefix}-transaction-search-xray",
                policy_document=json.dumps(
                    xray_logs_resource_policy(self.account, self.region)
                ),
            )

            self.trace_destination = cr.AwsCustomResource(
                self,
                "TraceSegmentDestination",
                # No onDelete: see the caveat above.
                on_update=cr.AwsSdkCall(
                    service="XRay",
                    action="updateTraceSegmentDestination",
                    parameters={"Destination": TRACE_DESTINATION},
                    physical_resource_id=cr.PhysicalResourceId.of(
                        f"{prefix}-trace-destination"
                    ),
                ),
                # UpdateTraceSegmentDestination also provisions the span log
                # groups and starts Application Signals discovery, so the caller
                # needs more than the two xray actions. Full set in infra_utils.
                policy=cr.AwsCustomResourcePolicy.from_statements(
                    [
                        iam.PolicyStatement.from_json(statement)
                        for statement in transaction_search_caller_statements(
                            self.partition
                        )
                    ]
                ),
                install_latest_aws_sdk=False,
            )

        for resource_name, resource_arn in (monitored_resources or {}).items():
            safe_name = resource_name.replace("-", "").replace("_", "").title()

            log_group = logs.LogGroup(
                self,
                f"Logs{safe_name}",
                log_group_name=f"/aws/bedrock-agentcore/{prefix}/{resource_name}",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=cdk.RemovalPolicy.DESTROY,
            )

            # Vended log delivery source
            source = cdk.CfnResource(
                self,
                f"Source{safe_name}",
                type="AWS::Logs::DeliverySource",
                properties={
                    "Name": f"{prefix}-{resource_name}-app-logs",
                    "ResourceArn": resource_arn,
                    "LogType": "APPLICATION_LOGS",
                },
            )

            # Vended log delivery destination
            dest = cdk.CfnResource(
                self,
                f"Dest{safe_name}",
                type="AWS::Logs::DeliveryDestination",
                properties={
                    "Name": f"{prefix}-{resource_name}-cw-dest",
                    "DestinationResourceArn": log_group.log_group_arn,
                },
            )

            # Connect source → destination
            cdk.CfnResource(
                self,
                f"Delivery{safe_name}",
                type="AWS::Logs::Delivery",
                properties={
                    "DeliverySourceName": source.ref,
                    "DeliveryDestinationArn": dest.get_att("Arn").to_string(),
                },
            )

        # ── Detective controls: alert on sensitive AgentCore API calls (item 7) ──
        # Requires CloudTrail management events (provided by the security stack). Routes
        # sensitive config-change events to an SNS topic for SOC/IR — end-to-end traceability.
        self.alerts_topic = None
        if enable_traceability:
            self.alerts_topic = sns.Topic(
                self,
                "SecurityAlerts",
                topic_name=f"{prefix}-agentcore-security-alerts",
                display_name="AgentCore security alerts",
            )
            rule = events.Rule(
                self,
                "SensitiveAgentCoreEvents",
                rule_name=f"{prefix}-agentcore-sensitive-events",
                description="Alert on sensitive AgentCore config-change API calls.",
                event_pattern=events.EventPattern(
                    source=["aws.bedrock-agentcore"],
                    detail_type=["AWS API Call via CloudTrail"],
                    detail={
                        "eventName": [
                            "CreateGateway",
                            "UpdateGateway",
                            "DeleteGateway",
                            "DeleteMemory",
                            "PutResourcePolicy",
                            "DeleteResourcePolicy",
                            "CreatePolicy",
                            "DeletePolicy",
                            "UpdatePolicyEngine",
                        ],
                    },
                ),
            )
            rule.add_target(targets.SnsTopic(self.alerts_topic))
            cdk.CfnOutput(
                self, "SecurityAlertsTopicArn", value=self.alerts_topic.topic_arn
            )

        cdk.CfnOutput(
            self,
            "MonitoredResources",
            value=",".join(monitored_resources.keys())
            if monitored_resources
            else "none",
        )
