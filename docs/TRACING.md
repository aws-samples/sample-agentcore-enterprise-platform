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

The setup above stops the rejected batches, and the pinned `aws-opentelemetry-distro==0.16.0`
delivers spans to the shared `aws/spans` log group — verified live: an invocation's trace was
queryable by traceId within ~2 minutes. Prove it end to end with:

```bash
python scripts/check_observability.py --spans   # needs an agent invocation in the last hour
```

Two things that look broken but are not:

- The classic X-Ray APIs (`batch-get-traces`, `get-trace-summaries`) return nothing for most
  traces. Transaction Search only *indexes* a sample (Default rule: 1%, free tier); span
  *search* — Logs Insights over `aws/spans`, which is what the CloudWatch Transaction Search
  console uses — sees 100%. An empty trace-API result is not a delivery failure.
- `aws-opentelemetry-distro>=0.18.0` is not needed for delivery or search. What it changes is
  routing: spans land in the agent's own log group instead of shared `aws/spans`. Cosmetic
  for this deployment, so the pins stay.
