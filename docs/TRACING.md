# Search Agent Traces

Runtime traces won't appear until the account is configured to receive them. This document covers the setup and how to verify it works.

## The problem

AgentCore runtimes send OTLP spans even when the account is not ready to receive them. If the X-Ray trace segment destination is still `XRay`, span batches get HTTP 400 responses and no trace appears. The deployment can still report success.

## What the observability stack does

The observability stack handles the two pieces described in [Enable transaction search](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Enable-TransactionSearch.html): a CloudWatch Logs resource policy that lets X-Ray write span log groups, and a trace segment destination of `CloudWatchLogs`.

## Verify the setup

```bash
python scripts/check_observability.py   # Check destination, span policy, and log deliveries
```

## Account setting

This is account and Region scoped, not stack scoped. If a platform team manages tracing elsewhere, deploy with `-c enable_transaction_search=false`. Destroying this stack does not change the destination back because other workloads may be using it.

## Span visibility

The setup above stops the rejected batches. To deliver spans to an agent's own log group instead of shared `aws/spans`, use `aws-opentelemetry-distro>=0.18.0` in the agent image. The runtime currently includes `0.16.0`, which ignores span-destination configuration. Transaction Search also changes CloudWatch span-ingestion pricing, with 1% indexed for free.
