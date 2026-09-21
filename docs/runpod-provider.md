# Auto Interpretability Lab: RunPod provider boundary

[Project overview](../README.md) · [Validation status](validation.md) · [Deployment checklist](live-deployment-plan.md)

This guide describes Auto Interpretability Lab's implemented provider boundary,
not a claim that model acceptance has passed. The adapter supports short,
explicitly supervised acceptance runs. Unattended
launches remain disabled: RunPod's `stopAfter` field has not yet been demonstrated
to stop billing after the controller host disappears. A local watchdog survives a
controller process crash, but shares the host's power and network failure modes.

## Supported operations

- Create a fresh, single-GPU Secure Cloud Pod from a digest-pinned image, attaching
  an existing Standard network volume of the approved size and data center.
- A separately approved infrastructure diagnostic may set
  `storage_mode="ephemeral_preflight"`, `volume_id=null`, and `volume_gb=0`.
  It binds the exact image and launch configuration and creates no retained
  volume. That worker cannot later acquire a research-job allowance; research
  creation and replacement use their own explicit storage contract.
- Research may set `storage_mode="disposable_research"`, `volume_id=null`, and
  `volume_gb=0`. This creates no persistent storage. The pinned image supplies
  public model assets; canonical outputs are fetched, verified and sealed on the
  controller before success. Incomplete output lost with a Pod is failed or
  inconclusive. It never silently resumes or replays.
- Quote the GPU price from the live catalog, add a conservative container-disk
  charge, and count all account Standard network volumes and retained Pod disks.
- Submit one GraphQL `podFindAndDeployOnDemand` mutation with `deployCost` below
  the approved total hourly ceiling and `stopAfter` equal to the already committed
  deadline. A new approval cannot extend an existing interval.
- Reconcile uncertain creation using a durable logical worker ID, request ID and
  configuration hash in Pod metadata. A timeout never triggers another create.
- Stop by permanently deleting only Pods associated with durable owned creation
  intents, using REST v2 `DELETE /pods/{id}`, then obtain a separate readback.
  This removes the Pod's disposable disks; its independently managed network
  volume survives. A successful DELETE response is not itself stop evidence.
  Absence of a previously observed, durably bound Pod confirms release; an
  unbound or newly created Pod's first 404 remains uncertain. `ERROR`, nonzero
  billed cost and inconsistent runtime remain uncertain.
- Replace a stopped worker with a fresh approved Pod. For disposable research,
  a verified absent Pod requires the matching durable STOPPED request and physical
  identity before replacement. Persistent deployments retain their network volume.
  Resume is disabled because its API lacks an atomic purchase ceiling.

No network volume is created or deleted by this adapter. High Performance or
unknown storage tiers anywhere in the account require a separate reviewed rate
implementation and currently block purchase. Published Standard storage rates
are pinned by an administrator with a maximum age of one day; the API exposes
volume sizes and tiers, not storage prices. The 28-day denominator and omission
of large-volume discounts make the daily estimate conservative. Other account
products such as Global Volumes need an explicit inventory/pricing extension
before this can claim a whole-account idle-cost guarantee for those products.

## Identities and files

The controller and the independently supervised credential broker run under the
trusted service UID. Only that UID can read the API key (owned by that UID, mode
0600). The config is root-owned, readable by the service, and not writable by
group or others. The watchdog has a separate UID and no provider key. Its Unix
broker endpoint accepts only that UID and only `status` / `stop` for logical
worker IDs; it has no start, create, credential or arbitrary HTTP method.
Here `stop` terminates the Pod rather than keeping a stopped Pod for later reuse.
An uncertain create's single matching inventory entry is verified and bound
before deletion. Conflicting or duplicate matches remain uncertain after cleanup;
they never authorize another purchase automatically.

This is an operating-system credential boundary. RunPod's documented API-key
permissions do not provide a Pod stop-only scope. The controller's account key is never sent to the research/MCP process or GPU
container. RunPod may inject its own Pod-scoped key; the trusted worker launcher
removes inherited credentials before starting SSH or execution processes. The launch environment accepts
only a dedicated Ed25519 `PUBLIC_KEY` and an optional delegated cgroup path.

The provider SQLite database contains creation intents, deployment configuration,
public SSH keys, absolute deadlines and physical Pod bindings. It contains no raw
API key or private SSH key. Back it up with SQLite's online backup API. A ledger
snapshot and provider snapshot are separate transactions; after restoring either,
keep dispatch disabled until live inventory reconciliation has completed.

## Service commands

The controller uses `--provider-config /etc/probe-core/runpod.json` in place of its
simulator's `--provider-state`. The watchdog uses `--stop-socket` and
`--stop-server-uid` in place of `--provider-state`. Start the broker independently:

```sh
python -m probe_core.runpod_provider serve-stop \
  --config /etc/probe-core/runpod.json \
  --socket /run/probe-provider/stop.sock \
  --watchdog-uid WATCHDOG_UID --socket-gid SOCKET_GID
```

`python -m probe_core.runpod_provider inspect-config --config ...` displays
capabilities and the exact launch configuration hash without returning the
credential. Starting the services or inspecting config does not buy compute.

For diagnostic infrastructure only, a human invokes controller admin method
`request_preflight` with `--preflight-file`. The JSON contains `deployment`,
`script_sha256`, and `max_runtime_seconds`. The returned request has an empty job
list and binds the diagnostic script hash and deployment. A separate admin
`approve` invocation consumes it. The core records its purpose as
`infrastructure_preflight`; it cannot authorize a research job, and a research
nonce cannot be consumed as an infrastructure allowance.

## Provider evidence checked on 2026-09-19

- [Live REST v2 OpenAPI](https://api.runpod.io/v2/openapi.json): create has no
  purchase ceiling/deadline/idempotency input; Pod actions accept an action only;
  inventories are cursor-paginated. Live observations showed that `EXITED` can
  coexist with a nonzero reported cost, so container state alone does not
  establish release.
- [Terminate a Pod](https://docs.runpod.io/api-reference-v2/pods/terminate-a-pod):
  permanent termination releases compute. The adapter uses this operation
  because Pod resume is unsupported and canonical results belong on the controller.
  Repeated DELETE is safe for the exact owned identity; conflicts and transport
  errors remain uncertain until a separate positive readback.
- [GraphQL schema](https://graphql-spec.runpod.io/): initial on-demand create has
  `deployCost`, `stopAfter` and `terminateAfter`; resume has neither a purchase
  ceiling nor a deadline. The published Pod object provides no deadline readback.
- [Official legacy CLI create source](https://github.com/runpod/runpodctl/blob/main/cmd/pod/createPod.go):
  `deployCost` is described as an hourly price ceiling.
- [API-key permissions](https://docs.runpod.io/get-started/api-keys): restricted
  per-resource scopes described there apply to Serverless endpoints.
- [Pod management](https://docs.runpod.io/pods/manage-pods): the scheduled-stop
  recipe is a local sleep followed by a stop request. It is not evidence of a
  provider-owned timer. The installed CLI's create help exposes no stop-after or
  terminate-after flag, despite a stale example in the plugin's volume guide.
- [Storage pricing](https://docs.runpod.io/pods/pricing): reviewed published rates
  are separate from live inventory and GPU quote responses.

A successful short timer trial can supply useful acceptance evidence. It does
not establish an outage guarantee or make the unattended mode available by itself.
