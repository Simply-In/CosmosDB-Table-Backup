# Contributing

Use English for issues, pull requests, code documentation, and contributor guidance. Search existing issues and PRs before proposing work. The [README](README.md), [backup format](docs/backup-format.md), and [security invariants](docs/security.md) describe the current contracts; proposals in issues are not implemented behavior.

## Development and validation

Use Python 3.14 and the pinned `uv.lock`. CI currently uses uv 0.12.19. From the repository root:

```bash
uv sync --frozen --all-extras --no-install-project --no-build
export PYTHONPATH=src
uv lock --check
uv run --frozen --no-sync --no-build ruff format --check .
uv run --frozen --no-sync --no-build ruff check .
uv run --frozen --no-sync --no-build mypy src
uv run --frozen --no-sync --no-build pytest
uv run --frozen --no-sync --no-build pip-audit
uv run --frozen --no-sync --no-build bandit -q -r src
```

The pytest configuration enforces 90% coverage. For a focused regression test, select the relevant test file and use `--no-cov` when whole-package coverage would not be meaningful, for example `uv run --frozen --no-sync --no-build pytest --no-cov tests/unit/test_storage.py`. Run the full coverage suite before considering an application change ready. Do not regenerate the lockfile or update dependencies incidentally.

For Bicep changes, follow the restore/lint/build and parameter checks in [validate.yml](.github/workflows/validate.yml). Container changes must pass its container build. These checks are not permission to deploy Azure resources. Use the smallest relevant existing checks during iteration and disclose every check not run. Documentation-only changes require local links and `git diff --check`; JSON catalogs and YAML forms also need parsing and consistency checks, not application tests.

## Report or propose work

Choose a [bug report](.github/ISSUE_TEMPLATE/bug_report.yml), [feature/improvement](.github/ISSUE_TEMPLATE/feature_request.yml), or [maintenance/research task](.github/ISSUE_TEMPLATE/task.yml). Blank issues remain enabled for maintainers, but must carry equivalent applicable sections. API-created issues must use the selected form's field labels as Markdown headings and answer all sections. Use `None` for absent dependencies and `N/A` with a reason for unrelated requirements.

- Cite current code or reproducible, sanitized evidence. Separate observations and measured results from hypotheses, especially in AI-generated proposals.
- State the expected outcome, bounded scope, non-goals, measurable acceptance criteria, tests/evidence, compatibility/security constraints, and dependencies.
- Bugs need reproduction, environment, expected/actual behavior, and impact. If a reproduction is unavailable, explain the evidence and remaining uncertainty.
- Performance work needs a baseline and representative workload description (entity sizes/counts, page/block settings, synthetic partition distribution), elapsed time/throughput, RU/throttling, memory/scratch bounds, and comparison results. Never promise gains based only on asynchronous architecture.
- Check supported Azure SDK APIs and service limits against official documentation. SQL API examples do not establish Cosmos DB for Table behavior. Gate uncertain request-charge/paging/retry behavior behind explicit real-service validation.

Never include entity keys/values, tokens, SAS strings, connection strings, plaintext/wrapped encryption keys, or unsanitized SDK HTTP traces. Benchmark and integration evidence must use synthetic data and explicitly approved nonproduction resources; do not create cloud resources or run costly tests without authorization. Do not publish exploit details publicly. No private disclosure contact is currently configured by this repository; use an established private maintainer channel if available rather than inventing one.

## Label catalog and policy

[`.github/labels.json`](.github/labels.json) is the canonical name/color/description catalog. Preserve existing GitHub labels; do not rename/delete them or add ad hoc synonyms. Amend the catalog and guidance together when changing policy.

### Primary work type

A triaged issue has **exactly one** primary type. A PR may have multiple applicable types. New maintenance/research tasks may omit a type until triage.

| Label | Use |
|---|---|
| `bug` | Confirmed or evidence-backed incorrect behavior, reliability, or scalability defect |
| `enhancement` | New capability, performance improvement, or research supporting it |
| `documentation` | Documentation-only correction or improvement |
| `dependencies` | Dependency maintenance; this is not an issue-blocking relationship |
| `question` | Support or clarification rather than a planned implementation |

### Priority

A triaged issue has exactly one priority; an untriaged issue may have none. A PR may have zero or one. Priority belongs in labels, not duplicated in titles. Assess impact and evidence, not merely dependency centrality.

| Label | Meaning |
|---|---|
| `priority:p0` | Active critical incident or confirmed data loss/integrity/security blocker; immediate release/operation blocker |
| `priority:p1` | High-impact correctness/scalability or selected foundational performance work |
| `priority:p2` | Normal planned improvement, tuning, or optional feature |
| `priority:p3` | Low-impact polish or deferred work |

An unmeasured optimization is not P0 just because an AI proposal calls it foundational. Maintainers may revise priority when new evidence changes impact.

### Areas and improvement themes

Assign at least one area to triaged issues and PRs; more than one is allowed when the change spans boundaries. Themes are optional and do not replace type or priority.

| Label | Scope |
|---|---|
| `area:backup` | Discovery, backup streaming/orchestration, completion |
| `area:restore` | Isolated restore and recoverability verification |
| `area:storage` | Blob staging/commit, object limits and streaming |
| `area:serialization` | Typed encoding and deterministic digest processing |
| `area:infra` | Bicep, identity, networking and deployment integration |
| `area:ci` | Build, validation, release and GitHub Actions |
| `area:developer-experience` | Contribution guidance, templates, labels and repository tooling |
| `performance` | Throughput, latency, CPU, memory or scratch I/O |
| `observability` | Safe metrics, diagnostics, benchmarks and evidence |
| `ru-budget` | Cosmos Table charge accounting and optional backup RU control |

### Triage, community and resolution

| Label | Use |
|---|---|
| `status:needs-triage` | Awaiting maintainer assessment; remove after scope/type/area/priority review |
| `status:blocked` | An unresolved linked hard prerequisite prevents work; remove when the prerequisite is satisfied |
| `duplicate` | Link the canonical issue before closing the duplicate |
| `invalid` | Explain why the report does not apply before closing |
| `wontfix` | Record the reason for declining work |
| `good first issue` | Well-bounded work suitable for a new contributor |
| `help wanted` | Explicit request for contributor help |
| `accessibility` | Accessibility barrier or associated improvement |
| `python`, `docker`, `github_actions` | Optional ecosystem labels; do not replace area or primary type |

Triage includes searching for duplicates, validating claims, setting one primary type and priority plus relevant areas/themes, and distinguishing hard prerequisites from related work. The forms add `status:needs-triage`; they intentionally do not assign priority. Catalog labels must exist on GitHub before forms relying on them are available on the default branch.

### Dependencies and examples

Use actual issue links under **Dependencies and references**. Write `Blocked by #number` for hard prerequisites, `Related to #number` for coordination, and optionally add reciprocal `Blocks #number` links. A related issue or a later remeasurement is not automatically a blocker. Local planning IDs must not appear as published issue references. Remove `status:blocked` once every hard prerequisite is satisfied. The label is manual, not an automated GitHub workflow state.

Examples:

- Backup baseline: `enhancement`, `priority:p1`, `area:backup`, `performance`, `observability`.
- Blob limit defect: `bug`, `priority:p1`, `area:storage`, `area:backup`.
- RU integration awaiting accounting/pipeline work: `enhancement`, `priority:p2`, `area:backup`, `ru-budget`, `status:blocked`, with actual prerequisite links.
- Contribution documentation: `documentation`, `area:developer-experience`; optionally one PR priority.

### Non-destructive catalog synchronization

Repository label management requires explicit permission. Review catalog changes before applying them. This create-missing procedure uses `gh` and `jq`, preserves all existing label metadata, and never deletes unknown labels:

```bash
set -euo pipefail
repo=smereczynski/CosmosDB-Table-Backup
catalog=.github/labels.json
existing=$(gh label list --repo "$repo" --limit 1000 --json name)
jq -c '.[]' "$catalog" | while IFS= read -r entry; do
  name=$(printf '%s' "$entry" | jq -r '.name')
  if printf '%s' "$existing" | jq -e --arg name "$name" 'any(.[]; .name == $name)' >/dev/null; then
    continue
  fi
  gh label create "$name" --repo "$repo" \
    --color "$(printf '%s' "$entry" | jq -r '.color')" \
    --description "$(printf '%s' "$entry" | jq -r '.description')" || exit 1
done
gh label list --repo "$repo" --limit 1000 --json name,color,description
```

Compare the readback with the catalog. If existing metadata differs, review the difference first; use `gh label edit '<existing-name>' --repo "$repo" --color '<catalog-color>' --description '<catalog-description>'` only for a specifically approved metadata update. Do not bulk-overwrite labels, use `--force`, or delete labels absent from the catalog.

## Pull requests

Use the [PR template](.github/PULL_REQUEST_TEMPLATE.md); it defines summary, linked issues, scope/non-goals, validation, documentation/configuration, compatibility/security and a review checklist. Follow it for UI and API creation; preserve its comments and sections. Record exact commands/results and explain omissions rather than asserting unspecified tests passed.

Use `Closes #number` only when the issue's entire acceptance criteria are met; use `Refs #number` for partial progress or related work. Use `owner/repo#number` for references outside this repository. Apply relevant type/area/theme labels and at most one optional priority according to the rules above. Update directly related documentation; avoid unrelated cleanup.

Preserve AES-GCM framing and nonce uniqueness, create-only writes, manifest-last completion, exact protected `cards` exclusion, deterministic key/content verification, private/keyless networking, and separate backup/restore identities. Any proposed wire-format or cryptographic change requires explicit compatibility/versioning and security reasoning, not an incidental performance refactor. See [Copilot repository instructions](.github/copilot-instructions.md) for the same expectations applied to coding agents.
