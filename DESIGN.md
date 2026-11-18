# Design and rollout notes

## Storage granularity

Minecraft's current Anvil layout stores 32 by 32 chunks in an `r.<x>.<z>.mca`
region file. Tiering one chunk means rewriting region headers, sector offsets,
compression payloads, and related entity/POI data while the server may still
have the region open. Fiesta should tier a complete same-coordinate bundle:
terrain (`region`), entities (`entities`), and points of interest (`poi`).
Paper's external oversized-chunk `.mcc` sidecars in the terrain region
directory must also move with their parent region; the prototype maps these by
chunk coordinates using floor division, including negative coordinates. The
Overworld, Nether, and End use their own world folders.

For Fiesta's current layout the logical roots are:

| Dimension | Region root |
| --- | --- |
| Overworld | `world/` |
| Nether | `world_nether/DIM-1/` |
| End | `world_the_end/DIM1/` |

Each root may have `region/`, `entities/`, and `poi/` members. Player files,
plugin data, chat archives, and server configuration are outside this tier and
must never be copied to the cold store.

## Proposed lifecycle

1. Paper records the last time each chunk was loaded. A region is eligible only
   after every saved chunk in it has exceeded the cold-age threshold.
2. The storage integration acquires an exclusive region lease, verifies that no
   chunk is loaded and all saves have completed, closes cached file handles, and
   marks the bundle `UPLOADING` in a durable journal.
3. It sends the bundle to a private Luna store under a temporary name, fsyncs,
   verifies sizes and SHA-256 hashes, then atomically publishes a manifest.
4. It marks the bundle `EVICTING`, removes local files only after a second lease
   check, syncs the directories, and commits `REMOTE_ONLY`.
5. Before any chunk read from a `REMOTE_ONLY` region, Paper's async I/O path
   downloads all bundle members to temporary local files, verifies them, and
   atomically publishes them. Only then may region files be opened.
6. If restore fails, chunk loading fails closed and reports a staff-visible
   error. It must not fall through to normal missing-region creation.
7. After local publication and verification, the remote object can be removed.
   If cleanup fails, both copies remain and quota accounting marks the region
   `CACHED` until the next safe reconciliation.

## Quotas and policy

- Hard remote cap: configurable, with an initial world-region cap of
  80,000,000,000 bytes (80 decimal GB). Leave additional capacity for the
  proxy, logs, and operating-system use.
- Keep a configurable local free-space reserve. Pause pre-generation and all
  evictions if accounting is uncertain or the reserve would be crossed.
- Start with a 30-day cold threshold. The value must be configurable and
  observable; a region's modification time is not a substitute for access age.
- When a host filesystem has verified `relatime`/`atime` behavior, seed the
  initial catalog from the newest access time among that region's bundle
  members. Treat it only as a coarse baseline; use live Paper load events after
  instrumentation starts. If mount semantics are unknown or use `noatime`, age
  old files from tracking start instead.
- Select least-recently-used eligible region bundles when the local target is
  exceeded. Never evict spawn, configured protected regions, or a region with
  active players, tickets, pending saves, or loaded chunks.
- Store only dimension/region files. Do not store keys, credentials, playerdata,
  plugin state, market ledgers, private chat archives, or backups in this tier.

## Crash consistency

The journal is an SQLite database with full synchronous commits. Each operation
uses a unique immutable object generation and explicit states:

`LOCAL_ONLY -> UPLOADING -> EVICTING -> REMOTE_ONLY -> RESTORING -> CACHED -> LOCAL_ONLY`

Startup reconciliation must inspect both the journal and manifests. If the
remote object is missing or has a checksum mismatch, stop world startup for the
affected dimension and preserve every surviving copy for manual recovery. Never
delete the only verified copy. The local prototype tests the transfer boundary;
it cannot prove Paper's file-handle or save-queue integration.

## Paper integration boundary

Paper's chunk storage path uses a cached `RegionFileStorage` and Moonrise
region-file I/O hooks. The integration needs a per-region read/write lease and
must insert remote restoration before `RegionFileStorage` constructs or opens a
missing local file. Bukkit chunk events do not provide this guarantee: they run
after the storage decision and cannot prevent an empty region from being
created. Therefore the current project is not a Bukkit plugin and must not be
deployed as one.

The 1.21.11 Paper patch shows `moonrise$getRegionFileIfExists` returning a
cached `RegionFile`, then checking `Files.exists(regionPath)` and returning
`null` when absent. Restore must run before that missing-file return. Paper's
`RegionFile` also resolves oversized chunk data as `c.<chunkX>.<chunkZ>.mcc`,
so those sidecars are part of the region bundle. References:
[Paper 1.21.11 Moonrise region storage patch](https://github.com/PaperMC/Paper/blob/ver/1.21.11/paper-server/patches/features/0001-Moonrise-optimisation-patches.patch),
[Paper 1.21.11 external chunk file handling](https://github.com/PaperMC/Paper/blob/ver/1.21.11/paper-server/patches/features/0019-Attempt-to-recalculate-regionfile-header-if-it-is-co.patch).

## Deployment gates

1. Build against the exact Paper 1.21.11 source used by Fiesta; keep the patch
   separate from existing uncommitted Paper source changes.
2. Exercise a copied world with real region, entity, and POI files.
3. Inject termination after every journal transition and every file move.
4. Test incomplete upload, corrupted manifest, Luna disconnect, quota full,
   local disk full, concurrent load during offload, and player arrival during
   restore.
5. Measure chunk-load latency over the actual Oracle-to-Luna route. If it cannot
   meet the game's join/movement needs, reduce the cold-store scope or stop.
6. Test rollback on a copy, then schedule a maintenance-window deployment with
   a complete stopped-server recovery point.

## Current implementation boundary

The host-specific capacity, proxy health, Chunky progress, and world sizes are
tracked in the operator's private progress notes and are intentionally not
published with this generic design. No production world files are read,
transferred, or changed by this prototype.
