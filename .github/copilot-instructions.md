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

## Issue ownership, session branches, and pull requests

Apply this workflow to GitHub Copilot and any other coding agent following these repository guidelines. Require an authenticated GitHub CLI (`gh`) for this workflow, subject to the session's supported tools and authorization boundaries; report missing CLI access as a blocker. When a user asks you to take on an issue, automatically maintain its ownership and development links as part of that authorized work; do not wait for a separate reminder.

- Identify the human GitHub user who initiated the issue work in this session, using trusted session context. Use `gh api user --jq .login` only when the authenticated account is that human; never infer ownership from git author configuration, the issue reporter, the PR author, a bot, or a service token. If identity is ambiguous, ask for the human's GitHub login before assigning.
- At the start of issue work, add that human as an issue assignee using `gh issue edit <issue> --repo <owner/repo> --add-assignee <human-login>` or an equivalent supported tool. Preserve existing assignees unless the user explicitly requests removal. Repeat for each issue actually taken on, not merely referenced.
- For each issue taken on, link the actual session branch in GitHub's **Development** section, or report the specific linking blocker. A branch name containing the issue number, a local session association, or a comment is not a GitHub development link. Preserve the app-managed branch and worktree; do not create or check out a replacement branch just to establish the link. `gh issue develop` and the public GraphQL `createLinkedBranch` mutation document creation of a new linked branch, not attachment of an arbitrary existing branch. For an existing remote session branch, use `gh api` or another supported tool only after verifying that its API explicitly supports existing-branch linking. If no verified method is available, report CLI/API linking as blocked and request manual linking in the GitHub UI if that UI offers existing-branch selection; verify the result rather than assume the UI can complete it. Do not claim that installing `gh` alone resolves this limitation. Creating a new linked branch is appropriate only when creating the intended session branch is authorized and compatible with the session. PRs without an issue need no issue assignment or branch-to-issue link; record N/A. A branch-link blocker does not remove the PR assignment or closing-reference requirements: `Closes` can close an issue on merge without a separate branch link.
- Every PR produced by the session must include the same human as an assignee, even when the agent/bot authors it. Set the assignee at creation when supported; otherwise add it immediately afterward using `gh pr edit <pr> --repo <owner/repo> --add-assignee <human-login>` or an equivalent supported tool. Preserve existing assignees.
- In the PR template's **Linked issues** section, include `Closes #number` for each issue whose complete acceptance criteria are satisfied, and `Refs #number` for partial or related work. GitHub automatically closes a linked issue when the closing PR is **merged into the default branch**, not when the PR is closed without merging. For a stacked PR targeting another branch, retain the issue reference and ensure the final integration PR into the default branch carries the closing reference once all acceptance criteria are met; do not retarget the session merely to trigger closure.
- Verify issue and PR assignees by reading them back, verify the branch appears in the issue's Development links (for example, `gh issue develop <issue> --repo <owner/repo> --list`), and verify the PR body and target branch. Do not mark these steps complete without evidence. If permissions, identity, an unpublished branch, or tool capabilities prevent a step, report the exact blocker and request only the specific authorization or manual GitHub action needed. Never bypass access controls or silently substitute a bot as the owner.

These rules cover assignment and linking within user-authorized issue work or PR creation, not blanket permission to publish branches, create PRs, change access, merge PRs, or close issues manually.

## Acceptance checklist reconciliation after merge

Follow [Updating issue acceptance after a pull request](../CONTRIBUTING.md#updating-issue-acceptance-after-a-pull-request) for every issue advanced by user-authorized work. Whenever a merge notification, post-merge check-in or resumed related work reveals a merged PR, automatically reconcile the **issue body's acceptance checklist** before reporting completion; do not wait for a reminder. Verify the live PR state, merge commit, target branch and validation evidence. This is mandatory for partial delivery using `Refs`, not only PRs that close an issue.

Check only fully satisfied criteria; leave partial or unvalidated criteria unchecked with a concise linked progress/evidence note and remaining gates. Preserve criterion wording, unrelated content and other contributors' progress; reconcile current content before writing and avoid duplicate notes on repeated runs. Distinguish offline evidence, real-service validation and intermediate stacked-branch integration. Keep incomplete issues open. Read back the issue to verify the checklist, note and state; report exact blockers rather than silently skipping updates. Closure without merge, approval or passing CI is not evidence of delivery.

If handing off an open PR, explicitly record acceptance reconciliation as pending for the next observed merge or resumed work. This policy does not provide continuous monitoring while the agent is inactive or authorize merging, deployments, access changes or unrelated issue edits.

## Changes and validation

Make focused changes; preserve existing behavior and update directly related docs. Use pinned dependencies and the development commands in CONTRIBUTING. Run the smallest relevant existing tests and checks, and report exact commands/results and omissions. Never claim proposed settings or concurrency already exist. The completed synthetic experiments are retired; do not recreate their code, tests, infrastructure or execution procedures without a new explicit user request. Historical test results in documentation are evidence only, not a plan to repeat the experiments. Real Azure validation/deployment needs explicit authorization, approved nonproduction resources and non-sensitive data. Do not weaken encryption, verification, retry behavior, access boundaries, or acceptance gates for throughput.
