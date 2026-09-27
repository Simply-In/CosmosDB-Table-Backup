# Security invariants

> **Navigation:** [Business overview](../README.md) · [Deployment](deployment.md) · [Operations](operations.md) · [Backup format](backup-format.md) · [Infrastructure](../infra/README.md)
>
> This document is authoritative for release-blocking security and trust-boundary invariants.

The deployment must fail closed. These invariants are release blockers:

1. Runtime authentication uses managed identities only. Cosmos keys, storage keys, SAS tokens, client secrets, and credentials in settings are forbidden.
2. The source Cosmos account is read through its Table private endpoint. Blob and Key Vault are Private Link-only. During development, ACR additionally permits public OIDC-authenticated publishing while retaining its private endpoint for workload pulls; public access must be disabled before production acceptance.
3. The backup VNet has no peering to production or hub networks and no route through production firewalls or NAT gateways.
4. Public network and shared-key access stay disabled where supported, except for the documented development-stage ACR publishing path. Key Vault permits its `AzureServices` firewall bypass solely because ARM key lifecycle deployment is otherwise rejected; the public endpoint remains disabled and runtime access remains RBAC-scoped.
5. `cards` is rejected during discovery before a table client is opened. Discovery failure and zero included tables fail the run.
6. Each run gets a new 256-bit DEK. Every encrypted object and retry gets a unique random 96-bit AES-GCM nonce.
7. The backup identity may wrap but never unwrap the DEK. The exact versioned HSM KEK identifier is recorded.
8. An encrypted manifest is committed last and is the sole completion marker. Partial objects never represent a successful run.
9. Runtime identities have no control-plane Owner, Contributor, User Access Administrator, deployment, or role-assignment permissions.
10. Backup blobs cannot be overwritten or deleted by the runtime identity. Immutability is locked only after nonproduction validation.

## Required negative tests

- Cosmos entity create, update, and delete are denied.
- Blob overwrite and delete are denied.
- Key Vault unwrap and key-management operations are denied.
- Public DNS or public data-plane access cannot bypass Private Link.
- A run with only excluded tables fails.

GitHub Actions deployment identities are separate from runtime identities and scoped to their respective subscriptions. Production/source integration remains an independently reviewed deployment.
