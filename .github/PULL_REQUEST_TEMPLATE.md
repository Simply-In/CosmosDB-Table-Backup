## Summary

<!-- Explain the change, its motivation, and its bounded scope. -->

## Linked issues

<!-- Use Closes #number only for fully completed work; otherwise Refs #number.
Use owner/repo#number for another repository. Use None if no issue applies.
Automatic issue closure requires merging the closing PR into the default branch;
closing an unmerged PR does not close the issue. For stacked PRs, put the closing
reference in the final default-branch integration PR after full completion. -->

## Scope and non-goals

<!-- Include intentional exclusions and any remaining follow-up work. -->

## Validation

<!-- List exact commands and results, or Not run with a reason.
Performance changes need representative baseline comparisons, not assumed gains.
Azure checks must use approved nonproduction resources and synthetic data. -->

## Documentation and configuration

<!-- List docs and setting/default changes. Distinguish implemented behavior from proposals.
Use N/A with a reason when no documentation or configuration change applies. -->

## Compatibility and security

<!-- Explain relevant effects on format/restore compatibility, AES-GCM, deterministic
verification, manifest-last completion, create-only writes, protected cards exclusion,
private/keyless access, bounded resources, and safe telemetry. Use N/A with a reason. -->

## Review checklist

- [ ] Linked issues are accurate; closing references cover only completed acceptance criteria.
- [ ] This PR and issues taken on are assigned to the human conducting the work (including agent sessions); the actual session branch is linked in GitHub Development, or blockers are documented.
- [ ] Relevant tests/checks and unperformed validation are documented above.
- [ ] Directly related documentation and configuration are updated, or N/A is justified.
- [ ] No entity data, credentials, keys, or unsafe HTTP logs are included.
- [ ] Applicable type, area, and theme labels follow CONTRIBUTING; at most one priority is assigned.
- [ ] No unrelated changes or unapproved Azure deployments are included.
