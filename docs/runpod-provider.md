# RunPod provider boundary

The adapter supports short, explicitly supervised acceptance runs. Unattended
launches remain disabled: RunPod's `stopAfter` field has not yet been demonstrated
to stop billing after the controller host disappears. A local watchdog survives a
controller process crash, but shares the host's power and network failure modes.

## Supported operations

- Create a fresh, single-GPU Secure Cloud Pod from a digest-pinned image, attaching
  an existing Standard network volume of the approved size and data center.
- Quote the GPU price from the live catalog, add a conservative container-disk
  charge, and count all account Standard network volumes and retained Pod disks.
- Submit one GraphQL `podFindAndDeployOnDemand` mutation with `deployCost` below
  the approved total hourly ceiling and `stopAfter` equal to the already committed
  deadline. A new approval cannot extend an existing interval.
- Reconcile uncertain creation using a durable logical worker ID, request ID and
  configuration hash in Pod metadata. A timeout never triggers another create.
- Stop only Pods associated with durable owned creation intents, through REST v2,
  and obtain a separate readback. An unbound or newly created Pod's first 404 is
  uncertain. `ERROR`, nonzero billed cost and inconsistent runtime remain uncertain.
- Replace a stopped worker with a fresh approved Pod using the same network
  volume. Resume is disabled because its API lacks an atomic purchase ceiling.

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

This is an operating-system credential boundary. RunPod's documented API-key
permissions do not provide a Pod stop-only scope. The account key never enters
the research/MCP process or the GPU container. The launch environment accepts
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
  inventories are cursor-paginated. The stop action is documented to release
  compute. `EXITED`/`TERMINATED` costs are reported as zero.
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
