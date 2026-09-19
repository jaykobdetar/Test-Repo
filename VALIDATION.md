# Phase 2 verification

Verified on September 19, 2026 with Python 3.13.13, Pydantic 2.13.2,
SQLite 3.53.1, and pytest 9.0.3 on Linux.

**277 tests passed; exit status 0.** Warnings are configured as errors.

| Requirement | Implementation | Evidence |
| --- | --- | --- |
| Complete design manifest with pinned provenance | `RunManifest` and nested schemas | Serialization roundtrip, exact fixture shape, hash/version/numeric/timestamp rejection tests |
| Five bounded GPU primitives | Discriminated `JobSpec.operation` | Capture, patch, ablate, steer, fit-probe validation; executable fields/path traversal/resource excess rejected |
| Hypothesis lifecycle, predictions, N0–N3 | `HypothesisRecord`, typed preregistration plan, ledger transitions | Complete validation lifecycle with persisted passed replication evidence; frozen plan substitutions rejected |
| One-time approval token/runtime/price/expiration | `ApprovalNonce`, transactional registration/consumption | Replay, races, expiration, incorrect batch/Pod/price/runtime, secret persistence and separate shutdown acknowledgement tests |
| Local SQLite with WAL and 5000 ms timeout | Writer and read-only connection initialization | Required PRAGMA readback, known remote mount rejection, real WAL reader snapshot with concurrent writer commit |
| Thread-safe single writer and explicit transactions | Connection-owning writer thread, `BEGIN IMMEDIATE` | Concurrent submissions and multiple ledger instances; SQL trace asserts actual DDL/DML occurs inside transactions |
| Queue states and lease recovery | State transitions, attempts, heartbeats, fencing, stop acknowledgement | State graph, reopen persistence, expiration in each execution state, obsolete/wrong worker rejection, bounded infrastructure retries |
| Atomic accepted results | Sealed retained artifacts plus transactional manifest/status/event commit | Missing/changed/unsafe artifacts, injected rollback, original-source changes, persistent bundle locations, orphan reconciliation |
| Append-only SHA-256 audit chaining | Canonical records, previous-hash inclusion, SQL immutable-event triggers, JSONL projection | Hash recomputation, tamper/reorder/delete/partial-tail detection, thread/process writers, authoritative prefix and trusted-tip checks |
| Secret/credential key rejection | Recursive audit scanner | Nested/camelCase secret fields rejected; legitimate model token metadata permitted |
| Reliable restart and export failures | Durable events and recoverable JSONL projection | Export failure after SQL commit, startup reconciliation, concurrent exporter race, fatal rollback releases callers |
| Pydantic v2 and complete implementations | `model_dump`, `model_validate`, v2 validators | Full tests, syntax compilation, source inspection for legacy `.dict()` and placeholder markers |

Test breakdown:

- `test_schemas.py`: 122 tests.
- `test_audit.py`: 68 tests.
- `test_ledger.py`: 67 tests.
- `test_resilience.py`: 7 tests.
- `test_storage.py`: 13 tests.

The wheel was built using `python -m pip wheel . --no-deps --no-build-isolation`,
installed into a separate local target directory without network/dependency
installation, and imported from outside the source tree. The installed wheel
completed a full approved job lifecycle, verified retained artifact bytes, read
back WAL/timeout settings, and verified the audit chain against its trusted tip.
The Python files inside the wheel match the final source package byte-for-byte.

The tests simulate execution, failures, clocks, and provider facts. They do not
claim to validate live GPU resource enforcement, cloud authentication, a RunPod
deployment, or the later external watchdog. Those services are outside Phase 2.
