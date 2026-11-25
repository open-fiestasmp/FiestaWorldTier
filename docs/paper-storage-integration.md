# Paper restore and offload integration

The companion Paper 1.21.11 patch adds a narrow restore seam to
`RegionFileStorage`. When a terrain region is absent locally and has a marker
at `<dimension-root>/.world-tier/remote/r.<x>.<z>.json`, Paper calls the
`fiesta-world-tier restore` command before returning no data or creating a new
region. It also checks this marker before trusting Moonrise's negative cache.
The child receives an argument vector (no shell), a minimal environment, a
bounded timeout, and a private configuration path. A failed restore propagates
as an I/O error; it cannot silently become an empty world region.

Patch `0038` exposes an experimental staged
`MoonriseRegionFileIO.offloadRegion(world, regionX, regionZ, coldAgeMillis)`
API. Paper snapshots the full region bundle while holding a short exclusive
region lease and cross-process file lock, releases the lease during upload,
then reacquires it to verify access age and source identity before writing the
remote-only marker and evicting. If a player accesses or changes the region
during upload, it keeps the local files. Java/Python lock interoperation was
checked on Linux. Staging needs temporary local disk space equal to the bundle
size.

Install the Python package on the Paper host so the command is available at
`/usr/local/bin/fiesta-world-tier`. Place its configuration at
`/etc/fiesta-world-tier/config.json` with mode `0600`; keep the SSH private key
and pinned `known_hosts` file outside the server world and repository. Paper's
command/config paths and timeout can be overridden with these JVM properties:

```text
-Dfiesta.worldTier.command=/usr/local/bin/fiesta-world-tier
-Dfiesta.worldTier.config=/etc/fiesta-world-tier/config.json
-Dfiesta.worldTier.restoreTimeoutSeconds=300
-Dfiesta.worldTier.offloadTimeoutSeconds=3600
-Dfiesta.worldTier.accessWriteIntervalMillis=60000
```

Paper records region-level shared-I/O use in an atomically replaced timestamp
under `<dimension-root>/.world-tier/access/`. It syncs the first touch after a
cold interval and throttles repeated writes to the configured interval. A
missing timestamp is treated as hot. The CLI's `catalog` command seeds existing
local regions as hot if no trustworthy history exists; `candidates` reports
age-eligible regions and excludes configured `protected_regions`. The `plan`
command estimates complete local bundle sizes and selects the oldest eligible
regions until the configured `local_world_data_budget_bytes` is approached.
It reads an unlocked, advisory snapshot of remote objects/staging usage and
constrains the estimate by current quota headroom. It does not start a live-
region offload. Cataloging and planning persist local access metadata, but do
not offload regions. Each actual upload rechecks exact quota under the remote
writer lock. Set `cold_age_days` in the private JSON config; candidate selection
and staged offload use that value, with age checked under Paper's lease before
and after upload. Before measuring quota, `plan` cleans abandoned staging
records older than seven days. It removes a remote duplicate only if no
remote-only marker exists and the full local bundle remains.

The command validates the marker's dimension, coordinates, and manifest hash,
checks the configured world root, verifies the complete downloaded bundle,
checks a local free-space reserve, atomically publishes all listed files, then
removes the marker and remote object. A marker is written only after a remote
bundle has been fully uploaded and verified. Before publishing restored files,
the command durably changes the marker from `REMOTE_ONLY` to `RESTORING`. Paper
will retry a `RESTORING` marker even if some local files exist, so a failed or
interrupted multi-file publication cannot be mistaken for a complete restore.

## Scope and current safety boundary

This hook covers demand restore and prevents missing remote data from being
treated as new terrain. The staged offload API uses Paper's OS lock for a short
snapshot and finalization, while the network upload runs without the region
lease. If a player accesses or changes a region during upload, final checks
cancel eviction and preserve its local files. The opt-in scheduler in patch
`0039` is disabled unless its target and interval system properties are set;
it defaults to idle-only mode. With
`fiesta.worldTier.schedulerAllowOnline=true`, it can try up to eight cold
regions while other areas remain active. Paper rejects loaded or ticketed
regions, and access-tracking failures disable offload. On a disposable Paper
server, the scheduler's tick dispatch and invocation of the staged API were
exercised using a fake CLI shim. The scheduler is not installed on Fiesta.
Do not enable offloading on a live world until budget selection,
restart/crash recovery, player movement, and concurrency behavior pass
disposable-server tests.

### Disposable transfer smoke test

On 2026-11-19, a patched Paper 1.21.11 server used a fresh disposable world
and an isolated Luna test directory. The scheduler offloaded a synthetic 8 KiB
region bundle. A corrected `World.getChunkAtAsync(..., generate=false)` probe
then requested the remote-only region. Paper restored the file; its SHA-256
matched the source, the durable marker cleared, and Luna test usage returned to
zero. A tick monitor saw no gap above 500 ms. The callback took 27.379 seconds
and returned `null`, because the synthetic region file contained no actual
chunk. This confirms marker-driven file restoration only; it does not prove
that Paper can load a real cold chunk or establish player-facing latency.

Earlier callback figures of 23.2 and 15.4 seconds came from a saved harness
that passed `generate=true`, despite earlier notes saying otherwise. They are
not valid no-generation restore measurements. A later saved-chunk test is
recorded below. All runs used isolated worlds and did not touch Fiesta data.

### Staged access and saved-chunk restore test

On 2026-11-19, a disposable patched Paper 1.21.11 server exercised the actual
staged offload hook and Python CLI using a local directory-object-store fixture.
The fixture contained saved chunks with known diamond and gold blocks. During
an 8-second delayed publish for the diamond region, the test requested the
existing chunk with generation disabled and verified its block. Paper's final
lease/access checks canceled eviction: the local region remained, no
remote-only marker was written, and no remote object was published. The maximum
observed tick gap during that upload was 71 ms. This demonstrates that this
Paper path preserved a region used during upload; it was an internal chunk
request, not a connected player session.

A separate saved gold-block region completed offload and demand restore. The
restore returned the existing chunk with its gold block intact, cleared the
marker, left the local region present, and removed the fixture's remote object.
This verifies saved-chunk serialization and restore against the local fixture.
It does not test Luna networking or player-facing latency. The earlier
27.379-second synthetic-region callback remains unexplained and is not
representative of this test. The scheduler is not installed on Fiesta.

### Luna endpoint integration test

On 2026-11-19, the same disposable Paper 1.21.11 harness ran against a
dedicated temporary object root on Luna, using the pinned SSH host key and a
short private `ControlPath`. During the staged upload of a 1.19 MiB saved
region, Paper loaded the chunk; remote usage showed the published object, then
final validation canceled eviction. The local region remained, no marker was
written, and abort cleanup returned remote usage to zero. Maximum observed
tick gap was 61 ms.

A second saved region (651 KiB) completed offload and demand restore through
Luna. The publish step took 17.065 seconds, finalization 0.534 seconds, and
the restore command 8.486 seconds. `getChunkAtAsync(..., generate=false)`
returned the saved gold-block chunk, the marker cleared, and remote usage
returned to zero. In an earlier run without SSH multiplexing, the same-size
region took 41.027 seconds to publish and 29.815 seconds to restore. Thus
multiplexing materially reduces connection overhead, but the remaining restore
time is still visible and needs improvement before a player-facing rollout.
The test used only synthetic disposable-world chunks; it did not connect a
player or touch Fiesta's live worlds. All temporary Luna roots were removed.

A repeat with SSH compression enabled took 16.604 seconds to publish and 8.632
seconds to restore, effectively unchanged from the multiplexed run without
compression (17.065 and 8.486 seconds). Compression remains configurable for
bandwidth-limited links, but these end-to-end measurements do not show a
reliable latency gain from it; the measured improvement came from connection
multiplexing.

A direct Luna SFTP benchmark downloaded the same 651 KiB synthetic region
twice with each buffer setting. The default 32 KiB buffer took 5.116 and 4.068
seconds; 256 KiB took 3.671 and 3.573 seconds. The adapter now uses the tested
256 KiB buffer for download-only batches via `sftp_download_buffer_size_bytes`.
Uploads retain OpenSSH's 32 KiB default because Luna rejected 256 KiB writes as
an oversized packet. This small download benchmark improved by about 21%;
repeat the full Paper restore cycle before treating that as an end-to-end
result. OpenSSH documents the round-trip and memory tradeoff for larger buffers
([manual](https://man.openbsd.org/sftp)).

With the larger download buffer and hash-guarded single-command cleanup, a
disposable 651 KiB store restore sequence took 0.50 seconds for the manifest,
6.00 seconds for fetch, and 0.57 seconds for remote cleanup (7.07 seconds
total). The checksum matched and remote usage returned to zero. This measured
the storage API, not a Paper callback or connected player; player-facing
latency remains unverified.

The test also exposed an OpenSSH Unix-domain socket path limit: a long
`ControlPath` failed before transfer. `OpenSSHConfig.validate()` now rejects a
template whose `%C`-expanded path exceeds 80 bytes, leaving room for OpenSSH's
temporary socket suffix. Keep the control socket directory private and the
path short.

If a process crashes during upload, local region files remain authoritative
and the staged snapshot is retained temporarily for recovery. Seven-day cleanup
removes an abandoned remote duplicate only when the local bundle is complete,
the manifest exactly matches the snapshot, and there is no remote-only marker.
If a durable marker exists, cleanup preserves the remote bundle.
The post-optimization Luna store-stage measurements total 7.97 seconds; the
larger synthetic-region Paper callback time remains unexplained. Later saved-
chunk testing against Luna passed, with an 8.5-second restore. Automatic
offloading remains disabled on Fiesta until restart/crash recovery, outage
behavior, and player-facing movement/latency are tested on disposable copies.

### Streamed object-store restore

On 2026-11-19, a disposable 4 MiB transfer benchmark to Luna measured warmed
median times of 4.291 seconds for SFTP and 1.365 seconds for `ssh cat`. The
object-store restore path now streams the concatenated bundle members over the
pinned SSH connection, splits them at manifest-declared byte lengths, caps the
trailing manifest at 1 MiB, and verifies each file's size and SHA-256 before
publishing it locally. Restore attempts still use a private staging directory;
a transient SSH disconnect can retry once. Upload and upload verification
remain on SFTP because Luna rejected larger SFTP write packets.

A live Luna smoke test then restored a disposable 4,390,912-byte, three-file
bundle in 2.437 seconds. All checksums matched, the test object was deleted,
remote usage returned to its 0-byte baseline, and no Fiesta world data was
read or changed. This is an object-store measurement, not a Paper callback or
connected-player result. Demand restore remains unapproved for live world
eviction until player-facing and crash/outage tests pass.

The integration is intentionally fail-closed. If the marker, command,
configuration, SSH host key, remote bundle, checksum, quota, or local disk
reserve check fails, the operation returns an I/O error and leaves the world
files untouched. Keep a separate complete backup before any production rollout.

### Filesystem-full restore check

On 2026-11-25, a disposable 64 MiB `tmpfs` held the local world and a
four-member 19 MiB region bundle. The object store and recovery journal were
outside the test filesystem. A test-only store filled the `tmpfs` after the
restore's free-space preflight, so the actual staged fetch hit `ENOSPC`.
Restore left the region in `RESTORING`, retained its remote object, published
no local terrain, and removed the partial stage. After the filler was deleted,
recovery restored all four files with matching SHA-256 hashes and cleared the
marker and remote object. The 102-test Python suite passed in the same checkout.

This exercises a real local filesystem exhaustion race against the directory
object store. It does not test Luna's shared filesystem or host quotas, SFTP,
Paper's chunk callback, or a connected player. Production eviction remains
disabled.
