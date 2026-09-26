# Backup format v1

A run uses a UUID backup ID and writes objects under `backups/<backup-id>/`. The only completion marker is a successfully committed `manifest.enc`; consumers must ignore a prefix without it. Every commit is create-only (`If-None-Match: *`). Table objects are numbered so table names are not exposed in object paths.

## Cryptography

Each run generates one random 256-bit data-encryption key (DEK). The exact versioned Key Vault key wraps it once with RSA-OAEP-256. Each independently encrypted object uses a fresh random 96-bit nonce, tracked for uniqueness during the run. AES-256-GCM object bytes are:

```text
"CTBE1" (5 bytes) || nonce (12 bytes) || ciphertext || tag (16 bytes)
```

Table AAD is canonical JSON containing `backup_id`, `kind: "table"`, `object_format: 1`, and its private manifest index. The manifest AAD is the exact byte sequence of `bootstrap.json`. Any change to bootstrap metadata therefore invalidates manifest authentication.

## Bootstrap envelope

`bootstrap.json` is canonical UTF-8 JSON and exposes only decryption bootstrap data:

- `backup_id` and `bootstrap_version`
- `object_format` and fixed encrypted-manifest name
- exact versioned `key_id`
- `wrap_algorithm` and base64 wrapped DEK
- base64 manifest nonce

It contains no table names, counts, hashes, entity keys, timings, or operational data. It is written only after all table blobs commit and immediately before the encrypted manifest.

## Entity stream

Each table plaintext is canonical UTF-8 JSON Lines. Each line has `version: 1` and a `properties` object. Every property is `{ "type": <tag>, "value": <value> }`. Supported tags are `String`, `Binary`, `Boolean`, `DateTime`, `Double`, `Guid`, `Int32`, and `Int64`.

Binary is canonical base64. DateTime is UTC ISO 8601 with six fractional digits and `Z`. GUID is lowercase canonical text. Int32 and Int64 remain distinct tags, including when values overlap. Non-finite doubles and naive datetimes are rejected rather than encoded lossily. PartitionKey, RowKey, and Timestamp use the same tagged representation.

## Encrypted manifest

The private manifest records application/format versions, table names, numbered object names, entity counts, encrypted-byte and plaintext SHA-256 hashes, order-independent canonical entity-key and entity-content SHA-256 hashes, timestamps, and the explicit consistency statement. For each entity, the implementation SHA-256 hashes the canonical key record and canonical persisted-content record independently, lexicographically sorts each table's fixed-size 32-byte digests, then SHA-256 hashes each sorted digest stream. Thus service enumeration order cannot affect verification. The key record covers typed `PartitionKey` and `RowKey`; the content record covers all persisted properties except the service-managed `Timestamp`. Each digest stream is externally sorted in fixed-size bounded-memory chunks beneath the application's writable working directory, then merged with bounded file fan-in; duplicates are preserved. Scratch runs are removed on success and error, so memory use is independent of table size while entity values remain streamed. The plaintext SHA-256 still authenticates the exact ordered backup object. The manifest is encrypted and committed last. Logical export is streamed per table and is not a cross-table point-in-time snapshot.

## Restore protocol

The deployed restore job invokes `python -m cosmos_table_backup.cli restore-test`. Its environment maps directly to `AZURE_CLIENT_ID`, `BACKUP_STORAGE_ACCOUNT_URL`, `BACKUP_CONTAINER`, `KEY_VAULT_KEY_ID`, `RESTORE_TARGET_COSMOS_ACCOUNT_RESOURCE_ID`, `RESTORE_TARGET_TABLE_ENDPOINT`, `RESTORE_SOURCE_ACCOUNT_RESOURCE_ID`, and `RESTORE_REQUIRE_ISOLATED_TARGET=true`. The source and target Azure resource IDs must differ after case and trailing-slash normalization, the target endpoint account must match the target resource ID, and the target must not share the backup Storage account name. These checks run before Azure data-plane access.

Restore discovery lists `backups/` and considers only prefixes with `manifest.enc`; partial prefixes are never opened. By default it selects the most recently modified completion marker; `RESTORE_BACKUP_ID` may pin an explicit UUID. It reads the canonical bootstrap, requires its key ID to be an exact version under the unversioned `KEY_VAULT_KEY_ID` supplied by infrastructure, and constructs the Key Vault crypto client from that authenticated envelope key ID—not from a synthesized or configured version. It then unwraps the DEK with RSA-OAEP-256, authenticates the manifest against the complete bootstrap bytes, and validates the manifest schema and numbered object paths.

After authenticating the manifest, restore enumerates the target's user tables and deletes every table absent from the manifest; Azure Table enumeration does not expose service-internal resources. Cleanup is fail-closed: enumeration/deletion failures or any residual unexpected table abort the run. Each table object is then read twice against the same Blob ETag with content validation. The first bounded pass verifies AES-GCM, manifest ciphertext size/SHA-256, and manifest plaintext SHA-256 before changing that manifest table. Restore deletes the table when present and recreates it, guaranteeing that stale entities cannot survive. The second conditional pass reconstructs typed JSON Lines and submits create-or-replace upserts grouped by PartitionKey. Cosmos Table transactions have a 2 MiB service limit, so both the 100-operation limit and a configurable conservative estimate capped at 1,500,000 bytes are enforced. The estimate uses doubled ASCII-escaped canonical JSON (including worst-case Unicode escaping) plus conservative per-operation OData/multipart overhead and batch framing. An entity that cannot safely fit is upserted individually. The second pass must reproduce the first pass hashes, byte count, and manifest entity count.

After writing, restore enumerates the actual target entities, recomputes order-independent canonical key and persisted-content hashes and the count, and requires all three to equal the encrypted manifest. It then re-enumerates target tables, removes any unexpected table that appeared during the run, re-enumerates again, and requires the exact table-name set from the manifest. Only then is a successful deterministic report produced. The report contains table/entity counts plus encrypted, plaintext, target-key, and target-content SHA-256 hashes; it never contains entity values. The restore identity needs Blob read/list, Key Vault unwrap, and isolated target Table delete/create/read/write permissions. It is separate from the backup identity. Conditional double reads assume immutable backup objects, as required by the backup retention policy.

## Failure semantics

Discovery failure, no included tables, serialization error, wrapping/encryption error, entity-read error, blob stage/commit error, or final-manifest error causes a non-zero backup exit. Partial/uncommitted objects may remain for retention cleanup, but no valid completion marker is produced. `EXCLUDED_TABLES_JSON` must be a JSON array of unique, non-empty table names. Those exact case-sensitive names are removed before any table client is opened, and the exact name `cards` is always excluded even if omitted from configuration. The backup CLI configures Azure Monitor with its managed identity and emits exact `backup.failed` and `backup.completed` identifiers; completion is emitted only after the encrypted manifest commit.

Restore configuration/guardrail failure exits 2. Missing completion markers, malformed bootstrap or manifest data, wrong keys, nonce/AAD/tag/hash/count mismatches, oversized records, changed Blob ETags, download failures, and target write failures exit non-zero without a success report. A failed restore can leave a partially populated recreated table; rerunning first deletes and recreates it, then performs the complete authenticated restore and final target enumeration.

The restore path configures Azure Monitor OpenTelemetry with the job's managed identity and `APPLICATIONINSIGHTS_CONNECTION_STRING`. It emits `restore.failed` for configuration, startup, authentication, validation, or target-write failures. It emits `restore.completed` only after every authenticated object has been restored and the deterministic verification report has been constructed. Events remain allowlisted and never contain entity values; these exact dotted names match the infrastructure `AppTraces` alerts.
