# Verified Google Drive research backups

Choose an owner-only Google Drive folder and pin its folder ID in the private
deployment configuration. The public repository contains no operator folder ID
or credentials. The existing Drive plan supplies storage; this workflow adds no
separate subscription. Backups use Drive's access controls without an additional
client-side encryption key. Keep the folder private.

`probe_core.backup` snapshots the live SQLite database through SQLite's online
backup API, including committed WAL data. It then copies exactly the accepted
immutable bundles referenced by that database and published retained inputs.
The archive includes a SHA-256 inventory, original manifest/bundle seals, source
commit, complete audit projection and trusted audit-chain tip. Concurrent future
database writes do not change the snapshot. Partially registered input directories
and orphan/unaccepted run outputs are excluded.

The installed snapshot service also takes a separate online SQLite snapshot of
`/var/lib/probe-provider/runpod.sqlite`, preserving creation intents and the mapping
from logical workers to actual Pods. This allowlisted state contains public launch
configuration, not the provider credential. Unsupported provider tables or secret
fields are rejected. The ledger and provider snapshots are individually consistent;
they are **not an atomic multi-database snapshot**. Restoration always requires
reconciliation against actual provider state before compute can resume.

The archive contains no configuration directory, environment dump, OAuth token,
RunPod key, SSH key, model cache or evaluator corpus. Current tooling rejects
accepted confirmatory/replication results until a separate evaluator-backup policy
is implemented. Models remain reconstructible from recorded exact revisions;
credentials must be recovered/reconnected separately.

## Produce and verify

The trusted identity owns the live ledger and snapshot outbox. The backup identity
can read completed archive files but cannot modify them or read the live ledger.
The snapshot service publishes mode `0640` archives into an outbox whose group has
read/traverse only. A manual invocation using the installed trusted account is:

```sh
python -m probe_core.backup create --ledger /var/lib/probe-core/research.sqlite --inputs /var/lib/probe-core/input-artifacts --provider-state /var/lib/probe-provider/runpod.sqlite --output /private/new-snapshot.tar --source-commit FULL_REVIEWED_SHA
python -m probe_core.backup verify /private/new-snapshot.tar --sha256 ARCHIVE_SHA256
```

Use enough local free space for the archive and an independent extraction/restore.
The default retained-data ceiling is 100 GiB. Large blobs are streamed, and archive
member counts, paths, sizes and checksums are validated; symlinks, hardlinks,
special files, duplicate names and traversal are rejected.

## Independent uploader

Only `probe-backup` owns `/var/lib/probe-backup/rclone.conf` (mode `0600`). The
administrator installer copies only the `gdrive` remote, excluding unrelated
configured storage accounts. OAuth refreshes may update this service-owned file.
Its token contents never enter command arguments, logs, receipts or the archive.

`probe-backup.service` invokes the snapshot producer first, then uploads completed
archives through rclone with an explicitly pinned Drive folder ID. Each object is
named by the immutable snapshot ID. The uploader downloads the full remote object,
verifies its SHA-256, SQLite integrity, audit chain and every accepted artifact,
then performs a separate offline restore before writing a success receipt.
Verification, transfer and restore first copy an archive into private staging
while hashing it, then consume that fixed copy. Replacing the original path
after its checksum is checked cannot substitute different restored or uploaded bytes.
An expired/revoked OAuth grant, transfer failure or altered download fails visibly;
none produces a success receipt. The reviewed administrator installer enables a
daily timer with up to ten minutes of jitter and catches up after downtime.
Existing exact archive hashes with verified remote receipts skip repeated transfers.
After a successful backup, a separate trusted retention service keeps the newest
two verified local archives. It removes only an older archive with a matching
successful remote/readback/restore receipt; unverified and unrelated files remain.
When an upload is pending, the producer retries it rather than accumulating new
daily archives. Remote snapshots are retained; no Drive delete operation is used.
Local space or remote quota failures remain visible and require operator action.

```sh
python -m probe_core.backup upload /private/new-snapshot.tar --config /var/lib/probe-backup/rclone.conf --drive-folder-id YOUR_PRIVATE_DRIVE_FOLDER_ID --receipts /var/lib/probe-backups/receipts
```

Keep the receipt's archive SHA-256 and audit tip with the operator's deployment
record. Hashes detect corruption; they are not protection against an attacker
rewriting both the archive and every trusted receipt. The separately controlled
Drive account and retained operator record supply the independent reference.

## Restore without starting compute

Download a selected archive into private local storage and use its independently
recorded SHA-256:

```sh
python -m probe_core.backup restore /private/download.tar /private/new-restore --sha256 ARCHIVE_SHA256
```

The destination must not already exist. Restoration verifies the original snapshot
before creating a relocated database. Scientific manifest documents/digests and
original audit events stay unchanged. Only retained artifact filesystem roots are
relocated in the new offline database; the immutable-manifest trigger is restored,
artifact files are read-only, and a new audited restore event records the source
snapshot and audit tip. The separate restore receipt records that no services
started. The result can be opened without any original artifact directory.

Before replacing production state, keep the restored system offline, reconcile
actual provider resources and unresolved attempts, recover credentials separately,
and obtain new compute authorization where required. A restored database must
never be treated as proof that an old paid worker stopped. Restore tooling never
starts a service, a cloud resource or an experiment.
