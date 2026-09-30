# Repository instructions for GitHub Copilot

## Authoritative guidance

Read [CONTRIBUTING](../CONTRIBUTING.md) before proposing or implementing changes. Use its labeling policy, the canonical [label catalog](labels.json), [issue forms](ISSUE_TEMPLATE), and [PR template](PULL_REQUEST_TEMPLATE.md). Complete every applicable template section; use N/A with a reason rather than omitting sections. Follow the same section headings when creating issues through an API. These instructions do not authorize GitHub mutations, deployments, or access changes on their own.

## Architecture and invariants

This is a Python 3.14 application with pinned Azure SDK dependencies, encrypted streaming backup, isolated restore validation, and Bicep infrastructure. Consult [backup format](../docs/backup-format.md) and [security invariants](../docs/security.md) as authoritative contracts.

- Never write to the production source or reinterpret restore validation as production restore.
- Exclude exact case-sensitive `cards` before opening its source table client; preserve configured exclusions.
- Preserve AES-256-GCM framing, nonce uniqueness, authenticated metadata, deterministic indices, and order-independent key/content verification.
- Commit `manifest.enc` last and only after every required operation succeeds. Partial prefixes are not successful backups.
- Preserve create-only Blob commits, private networking, keyless managed identities, and least-privilege role separation.
- Keep streaming memory, task/queue concurrency, and scratch resources explicitly bounded. Propagate errors and cancellation; do not fire and forget.
- Extend allowlisted telemetry only with safe fields. Never emit entity values or keys, secrets, connection strings, tokens, plaintext/wrapped encryption keys, or unsanitized SDK HTTP logs.

## Issues, evidence, and labels

Verify AI-generated proposals against the current implementation. Distinguish architectural observations, measurements, and hypotheses. Check official Microsoft documentation for Azure SDK signatures and service limits; SQL API behavior is not evidence for Table API behavior. Do not fabricate APIs, benchmark gains, or test results.

Search for duplicate issues before creating new work. Use English, bounded scope/non-goals, measurable acceptance criteria, validation requirements, compatibility/security considerations, and real dependency links. Record supported-SDK and real-service feasibility gates where behavior is not established.

Reuse catalog labels. For triaged issues, select exactly one primary type and priority and at least one area, with optional themes. New untriaged issues use `status:needs-triage` without inventing urgency. P0 is for critical incidents or confirmed integrity/security blockers, not merely foundational work. Keep priority out of titles. Use `status:blocked` only with an unresolved hard prerequisite; distinguish related work from blockers. PRs may carry multiple applicable types and at most one optional priority.

Use `Closes` only when an issue's complete acceptance criteria are satisfied; otherwise use `Refs`. Use actual issue numbers/URLs, never local planning IDs as published dependency references. Do not create/rename/delete labels ad hoc or broadly change workflow permissions.

## Changes and validation

Make focused changes; preserve existing behavior and update directly related docs. Use pinned dependencies and the development commands in CONTRIBUTING. Run the smallest relevant existing tests and checks, and report exact commands/results and omissions. Never claim proposed settings or concurrency already exist. Benchmark with synthetic data and approved nonproduction resources; real Azure validation/deployment needs explicit authorization. Do not weaken encryption, verification, retry behavior, access boundaries, or acceptance gates for throughput.
