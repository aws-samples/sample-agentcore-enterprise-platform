# v0.1.0 Known Limitations

Review these before an EBA or customer deployment.

1. **This is a sample baseline, not an AWS service or production
   certification.** The production profile has automated synthesis coverage,
   but no completed customer threat model, operational readiness review,
   backup/restore rehearsal, or production launch approval.
2. **Release evidence is narrower than configuration breadth.** The strongest
   live evidence is in `us-east-1`. Direct Entra ID, brokered Okta/Ping,
   distributed deployments, and other Regions require customer-specific live
   validation.
3. **Model metadata is not model invocation.** `deploy.sh doctor` checks that
   the configured model or inference profile is discoverable without incurring
   inference cost. Account entitlement, Marketplace prerequisites, runtime
   IAM, and quota are proven only by a successful post-build invocation.
4. **A private pre-built migration image cannot always be inspected.** The
   customer must prove a `linux/arm64` manifest, pin an immutable digest, and
   provide registry access where required. Source builds performed by the
   accelerator target arm64.
5. **Migration automation stops at the target runtime.** Customer data,
   existing private connectivity, DNS/private CA, event sources, traffic
   routers, cutover, and rollback execution are not modified. Readiness
   commands validate evidence and approval records only.
6. **No generic data-copy or trigger executor exists.** The built-in
   `retain-source-v1` adapter moves no records. Queue shadow consumers are
   specifically unsafe because they can steal production messages.
7. **Production supply-chain controls are incomplete.** Images are not yet
   signed, no release SBOM/provenance bundle is published, and immutable
   promotion of one artifact through dev, staging, and production is not
   implemented.
8. **AI quality evaluation is outside the current verifier.** There is no
   versioned customer evaluation set, prompt-injection campaign, quality
   threshold, or per-agent inference cost attribution.
9. **Some security controls require first-deployment calibration.** Exercise
   Cedar in `LOG_ONLY` before enforcing it, publish and pin the Bedrock
   Guardrail version for production, and verify interceptor event shapes
   against each live Gateway target.
10. **VPC mode is not air-gapped.** The default private subnets retain a NAT
    route for APIs and internet dependencies, and NAT gateways plus interface
    endpoints incur hourly cost. AgentCore network interfaces can delay
    networking-stack removal.
11. **Facilitator and accessibility qualification is incomplete.** The EBA
    flow has not yet completed three clean-account rehearsals by independent
    facilitators or a full keyboard, screen-reader, stale-state, and fallback
    review.
12. **The manifest schema has no independent version field yet.** Pin the
    accelerator release with the manifest, review generated `platform.yaml`
    reference changes during upgrades, and run `design` plus `diff` before
    applying a newer tag.

Track the remaining production work in the
[Production Readiness Plan](PRODUCTION_READINESS_PLAN.md) and use the
[support matrix](SUPPORT_MATRIX.md) for current evidence.
