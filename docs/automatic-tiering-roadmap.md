# Automatic hot/cold region tiering roadmap

This document describes the intended hybrid storage policy. It is a design and
implementation checklist, not a claim that automatic eviction is available on
Fiesta. The companion Paper patch supports marked demand restore, explicit
leased offload, region access timestamps, and an opt-in scheduler with a
per-region online mode. Python commands list age-based candidates and dry-run a
local world-data budget plan.
An isolated Paper probe restored a synthetic region file, but its
`generate=false` callback took 27.379 seconds and returned `null`; that run did
not contain a chunk. A later disposable Paper runtime test passed concurrent
access during staged upload and cold restore of a saved chunk, using a local
directory-object-store fixture. It proves those Paper paths with saved chunks,
but does not measure Luna networking or player-facing latency. Earlier 23.2-
and 15.4-second figures used a harness with `generate=true` and are superseded.
The scheduler is not installed on Fiesta. Do not enable automatic eviction on
a live world until the remaining gates below pass on a disposable copy.

## What moves

Minecraft stores 32 by 32 chunks in an Anvil region. The tiering unit is the
whole same-coordinate region bundle, not an individual chunk:

- terrain: `region/r.<x>.<z>.mca` and any matching `c.<chunkX>.<chunkZ>.mcc`
  oversized-chunk sidecars;
- entities: `entities/r.<x>.<z>.mca`;
- points of interest: `poi/r.<x>.<z>.mca`.

The bundle is tracked independently for the Overworld, Nether, and End. Player
data, plugin state, chat archives, configuration, and backups are not part of
this tier.

## Storage roles and limits

- Oracle remains the live Paper server and hot cache. It must retain a
  configurable free-space reserve for the OS, server writes, logs, and safe
  restores. If the reserve is threatened or accounting is uncertain, stop
  pre-generation and tier operations; do not delete files to recover space.
- Luna is a remote cold store and optional proxy host. Its use is capped by a
  configurable application quota; the Fiesta deployment target is at most
  90,000,000,000 bytes for region objects, within the requested 80–100 GB
  ceiling. Leave capacity for Luna's other services and filesystem overhead.
  Enforce an OS quota too when the host supports it.
- Track used bytes from published manifests and reconcile that number against
  the remote filesystem. A mismatch pauses new uploads and evictions.

The cap is a ceiling, not a target that must be filled. Remote bundles remain
immutable and checksummed. Keep the remote copy after restore until the local
bundle has been verified and the journal records a safe state.

## Cold-region selection

1. Record a region's last-use time from Paper chunk loads. Filesystem access
   times may seed an initial estimate only when mount semantics are verified;
   they are not the ongoing source of truth.
2. Consider a bundle cold only when every chunk in it has been unused for the
   configured age (initial policy: 30 days), no chunk in the region is loaded,
   no player or ticket requires it, and no save, read, or write is pending.
3. Protect spawn and operator-configured regions. Do not infer safety from
   modification time or from a region being outside the current view distance.
4. When Oracle's world-data budget is exceeded, choose the least-recently-used
   eligible bundles. Re-evaluate eligibility under an exclusive Paper region
   lease immediately before removing local files.
5. If no eligible bundle can be leased, pause eviction and report the reason.
   Never evict a region simply to make a numeric world-size target appear met.

Because storage is organized in 32x32-chunk files, a single recent chunk keeps
its entire region bundle hot. That is intentional: rewriting individual chunks
to save space would risk corruption and is outside this design.

## Offload and demand-restore flow

1. Acquire an exclusive lease for the dimension and region. It must prevent
   new Paper reads/writes from racing the transfer, drain existing I/O, prove
   that all chunks are unloaded and unticketed, and close the cached region
   handles for terrain, entities, and POI.
2. Write a durable journal transition, upload the complete bundle to a
   temporary remote generation, and read it back to verify every size and
   SHA-256 digest.
3. Publish the immutable remote manifest. Only after the manifest and local
   journal are durable may Paper remove local files and record `REMOTE_ONLY`.
4. On a later chunk request, Paper must hold the region's I/O gate, fetch every
   bundle member to temporary files, verify the manifest and hashes, journal
   publication of the full local bundle, and allow the region file to open
   only after every member is validated and durable.
5. If a player reaches a cold region, loading waits for this restore. It may
   add latency, especially during a Luna outage or on a slow route; the server
   must fail closed on a failed restore rather than generate replacement
   terrain. Do not promise instantaneous movement into remote-only terrain.
6. Delete the remote generation only after the verified local copy and journal
   state are durable. If remote cleanup fails, retain both copies and reconcile
   quota later.

## Recovery and safety gates

The journal must recover interrupted upload, eviction, and restore operations
before world I/O starts. On ambiguity, preserve all surviving copies and block
the affected region. Do not choose a possibly incomplete local or remote copy
automatically.

Before any production eviction is enabled, a disposable copy must pass:

- region lease contention with chunk loads, saves, tickets, and player movement;
- restart or forced termination after every journal and file-publication step;
- missing, partial, and checksum-invalid remote bundles;
- Luna disconnects, quota exhaustion, Oracle low-space conditions, and
  rollback;
- restoration of terrain, entities, POI, and oversized-chunk sidecars;
- measured cold-region restore latency over the actual Oracle-to-Luna route;
- reconciliation when a restored local copy and remote copy both exist.

The first operational rollout should use a small, explicitly allowlisted set
of cold regions, observe it through normal play, and only then consider
automatic least-recently-used selection. A full world migration or bulk
offload is not part of the initial rollout.

## Implementation status

- Durable bundle journal, local fixture store, and pinned OpenSSH/SFTP adapter:
  implemented as a prototype.
- Paper demand-restore seam with fail-closed marked recovery: implemented in
  the companion Paper 1.21.11 patch; not installed on Fiesta.
- Paper region lease and explicit offload API with cross-process lock: implemented
  experimentally in the companion patch; the scheduler can try up to eight
  candidates and optionally run while other regions are active.
- Region I/O access timestamps, initial catalog, age-based candidate listing,
  and a dry-run planner for a configured local world-data target: implemented
  experimentally. The planner reads an unlocked remote-usage snapshot to cap
  its estimate; each actual upload rechecks quota under the remote writer lock.
- Opt-in scheduler: implemented in the companion Paper patch, disabled by
  default, and not installed on Fiesta. Online mode rejects loaded or ticketed
  regions. It snapshots under a short exclusive lease, uploads without that
  lease, then reacquires it to recheck age and source identity before eviction.
  A player can use the local region during upload; access or changes cancel
  eviction. A disposable Paper 1.21.11 run requested a saved diamond-block
  chunk during an 8-second upload; eviction was canceled, the local region
  remained, and the maximum tick gap was 71 ms. A separate saved gold-block
  region offloaded and restored on demand, with the block intact and marker
  cleared. Both used a local directory-object-store fixture. Luna-network
  behavior, actual player movement/latency, clean byte-level post-save
  comparison, crash recovery, and outage behavior remain unverified.
- Disposable Paper-server integration and production rollout: not complete.

See [DESIGN.md](../DESIGN.md) for the overall state machine and
[Paper storage integration](paper-storage-integration.md) for the current
Paper patch boundary.
