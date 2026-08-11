# Contributing Guidelines

Thank you for your interest in contributing to the AgentCore Platform and
Security Accelerator. Whether it's a bug report, new feature, correction, or
additional documentation, we greatly value feedback and contributions from our
community.

Please read through this document before submitting any issues or merge
requests to ensure we have all the necessary information to effectively
respond to your bug report or contribution.

## Reporting Bugs/Feature Requests

When filing an issue, please check existing open and recently closed issues to
make sure somebody else hasn't already reported it. Please try to include as
much information as you can. Details like these are incredibly useful:

- A reproducible test case or series of steps
- The version of the code being used
- Any modifications you've made relevant to the bug
- Anything unusual about your environment or deployment

## Contributing via Merge Requests

Contributions via merge requests are much appreciated. Before sending us a
merge request, please ensure that:

1. You are working against the latest source on the `main` branch.
2. You check existing open and recently merged merge requests to make sure
   someone else hasn't addressed the problem already.
3. One merge request addresses one scoped task — no drive-by fixes. Open a
   separate issue for anything else you notice.

To send us a merge request:

1. Create a branch from `main` named for the change (`fix/...`, `feat/...`,
   `ci/...`, `chore/...`).
2. Modify the source, focusing on the specific change you are contributing.
3. Verify locally: the CI pipeline (`.gitlab-ci.yml`) runs ruff on changed
   Python files, control-library validation, hadolint on agent Dockerfiles,
   pytest, `cdk synth`, and terraform validate. Run the relevant subset before
   pushing (see `Makefile` targets and `docs/TESTING.md`).
4. Commit with a clear message: imperative subject, a body explaining what
   and why, and verification results.
5. Push and create a merge request; answer any CI failures or review
   discussion.

## Security

The security controls in `control-library/` are customer-facing reference
policies. Changes to SCPs, Cedar policies, IAM policies, or VPC endpoint
policies require extra care: validate semantics against AWS documentation
(especially IAM condition-operator behavior in Deny statements) and test in a
sandbox account/OU before proposing enforcement defaults.

Do not commit account IDs, tenant/client IDs, endpoints, credentials, or any
customer-identifying values. Use documentation placeholders
(`111122223333`, `o-example123`, `<your-tenant-id>`).

If you discover a potential security issue, do not create a public issue —
contact the maintainers directly.

## Licensing

See the [LICENSE](LICENSE) and [NOTICE](NOTICE) files for this project's
licensing. We will ask you to confirm the licensing of your contribution.
