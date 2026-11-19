# Fiesta World Tier

Experimental, fail-closed region-file tiering for a Paper-based Minecraft world.

This repository is a lab prototype. It is **not ready for a live world** and does
not modify Fiesta's server. A plugin alone cannot safely move live `.mca` files:
the server owns region-file handles and performs chunk reads and writes inside
its storage layer. Production use requires a Paper storage integration that can
lock a whole region bundle, wait for pending saves, restore a cold bundle before
opening it, and stop rather than generate a replacement if restore fails.

The prototype models the durable transfer state machine and tests it against
disposable worlds and a local object-store fixture. It includes an optional
OpenSSH/SFTP adapter that requires a pinned host key and a restricted-permission
identity file. Automated transport tests use a local fake. After the batched
transport change, a disposable Paper 1.21.11 server restored a synthetic
spawn-region bundle from Luna after `/forceload add` requested the marked
chunk. The restored region SHA-256 matched the original, the durable marker
cleared, and the remote test object was consumed; the unique test root was
then removed. This test used an
isolated world and did not touch Fiesta world data. Player-facing restore
latency, outage recovery, concurrent player movement, and production-scale
transfers still need testing. The project never scans, copies, or deletes the
live Fiesta world.

An earlier isolated integration exercised Paper's idle scheduler, leased
offload API, Luna SFTP storage, and demand restore with a synthetic 8 KiB
region. Its `generate=false` callback took 27.379 seconds and returned `null`
because the region contained no actual chunk. The checksum matched and the
marker cleared, but that run only proved marker-driven file restoration. Its
callback timing remains unexplained and is not a player-latency measurement.
The disposable run used a zero-byte free-space reserve; no Fiesta data moved.

A later disposable Paper 1.21.11 runtime test used saved chunks and a local
directory-object-store fixture. During an 8-second staged upload, Paper loaded
an existing diamond-block chunk; final validation canceled eviction, leaving
the local region intact with no marker or published remote object. The maximum
observed tick gap was 71 ms. A separate saved gold-block chunk completed
offload and demand restore; Paper returned the existing chunk, the marker
cleared, the local region was present, and the fixture's remote copy was
removed. This validates staged Paper access and saved-chunk restore against a
local fixture. A later disposable run exercised both paths against Luna's
actual SFTP endpoint: saved-region restore passed, but took about 8.5 seconds.
No player session was connected. Crash/outage recovery, player-facing latency,
and production-scale transfers remain open.

A disposable Paper 1.21.11 run then exercised both paths against a dedicated,
temporary object root on the actual Luna host. With SSH connection
multiplexing enabled, the concurrent-access case uploaded a 1.19 MiB region,
then canceled eviction and removed the remote duplicate after the chunk was
used; the local region remained and the maximum tick gap was 61 ms. A separate
651 KiB saved region offloaded and restored with its gold block intact, marker
cleared, and remote usage back to zero. Publish took 17.1 seconds and restore
8.5 seconds. Without multiplexing, the same saved-region cycle took 41.0
seconds to publish and 29.8 seconds to restore. This is a real Luna transport
test, but still an internal chunk request rather than a player session; 8.5
seconds is too slow to call seamless. Test roots were removed and no Fiesta
world data was transferred. Keep automatic eviction disabled until further
latency and recovery work passes.

A follow-up changed tiny manifest reads from SFTP downloads to stdout over the
same pinned SSH connection. A real Luna smoke test with another synthetic
8 KiB bundle measured 0.50 seconds for the manifest read, 5.40 seconds for the
verified fetch, and 4.43 seconds for hash-guarded remote deletion. A second
pass moved hash-guarded cleanup from SFTP to one SSH command under the remote
writer lock; the real Luna smoke test then measured 0.50 seconds for the
manifest read, 5.45 seconds for verified fetch, and 2.02 seconds for deletion.
Both SHA-256 checks passed and both unique test roots were removed. These
store stages total 7.97 seconds. A post-optimization Paper probe took
27.379 seconds end to end and returned no chunk object because the fixture
region contained no chunk. The gap between store-stage and Paper callback
timings remains unexplained and needs investigation before player-facing use.

A direct Luna SFTP benchmark downloaded the same 651 KiB synthetic region
twice with each buffer setting. The default 32 KiB buffer took 5.12 and 4.07
seconds; a 256 KiB buffer took 3.67 and 3.57 seconds. The adapter now uses the
tested 256 KiB buffer for download-only SFTP batches
(`sftp_download_buffer_size_bytes`). Uploads retain OpenSSH's 32 KiB default: a
real Luna probe rejected 256 KiB writes as an oversized packet. The download
benchmark improved by about 21%; the full Paper restore cycle still needs a
repeat with the larger download buffer.

With the larger download buffer and hash-guarded single-command cleanup, a
disposable 651 KiB store restore sequence took 0.50 seconds for the manifest,
6.00 seconds for fetch, and 0.57 seconds for remote cleanup (7.07 seconds
total). The checksum matched and remote usage returned to zero. This measured
the storage API, not a Paper callback or connected player; the live player
latency gate remains open.

The OpenSSH adapter enforces a configured application byte cap across its
objects and staging directories; this is not an OS-level filesystem quota.
Use a server-side quota as a second limit where the host supports it. A sample
deployment caps region objects at 80,000,000,000 bytes and leaves additional
capacity for proxy and system data. The companion Paper 1.21.11 patch provides
marked demand restore, per-region I/O leases, and an experimental
`offloadRegion(...)` API that copies a bundle under a short Paper lease, uploads
the independent copy without the lease, then reacquires the lease to verify
the source and evict it. The CLI requires proof of Paper's cross-process lock
for both lease-bound phases; Java/Python lock interoperation was verified on
Linux. Staged copies need temporary local disk space, and the planner now
cleans abandoned stage records after seven days while preserving any marked
remote-only bundle.
The companion Paper patch includes an opt-in scheduler with idle-only default
and an online mode that checks up to eight candidates against Paper's region
lease. If a player enters the candidate while its snapshot uploads, the final
lease, access-age, and source checks cancel eviction and preserve the local
region. The staged path passes Python lifecycle tests and a Paper compile. A
disposable concurrent-access and saved-chunk restore test passed with a local
directory-object-store fixture. A separate disposable Luna test also passed
concurrent access, saved-chunk offload, and restore. Online mode is not
installed or tested on Fiesta. Crash/outage recovery and player-facing latency
still need testing before any production offload.

After the verified-fetch batching change, a separate 1,048,607-byte synthetic
region bundle passed publish, manifest/hash read, verified fetch, and guarded
delete against Luna with SSH multiplexing enabled. Measured times were 26.6 s,
3.1 s, 9.4 s, and 9.0 s respectively; the unique remote test directory was
removed and usage returned to zero. A later disposable Paper 1.21.11
`/forceload add` test exercised the patched restore hook with a synthetic spawn
region; its checksum matched, the marker cleared, and the unique remote root was
removed. One earlier multiplexed store attempt timed out while later complete
transfers passed, so retry and outage behavior still need testing. Player-facing
restore latency remains an open requirement.

A cold-region fetch now retries once after an SSH disconnect (exit code 255).
Each attempt downloads into a private temporary directory and publishes files
only after the bundle and manifest verify. Timeouts and SFTP command errors are
not retried; a failed restore leaves the recovery marker and remote bundle for a
later attempt.

After this change, a real 1,048,607-byte synthetic Luna transfer completed in
20.14 seconds for publish and 6.18 seconds for verified fetch. The restored
SHA-256 matched, the remote object was deleted, and the unique test root was
removed. This was a clean transfer with no retry needed.

## Safety requirements

- Tier complete region bundles, not individual chunk NBT records. A bundle
  consists of the same `r.x.z.mca` in the dimension's `region`, `entities`, and
  `poi` directories when those files exist, plus any `.mcc` oversized-chunk
  sidecars belonging to that region.
- Mark a region cold only when all of its chunks have been unused for the
  configured age, no chunk is loaded, and no save or region-file operation is
  in flight.
- Upload and verify a complete immutable bundle before removing any local file.
- Restore and verify a complete bundle before Paper is allowed to open it. A
  failed or partial restore must block that region; it must never look like a
  new empty region.
- Bound world-tier objects with an application byte cap and enforce a separate
  server-side quota where available. Keep credentials outside this repository
  and transfer only region data over SSH.
- Keep the remote copy until a verified local restore is complete. Recover
  interrupted transactions from the journal before accepting chunk I/O.

## Prototype

`src/fiesta_world_tier/` contains the manifest, local fixture store, pinned
OpenSSH/SFTP store, journal, access catalog, marker lifecycle, and restore and
offload commands. Paper writes a durable region-level touch timestamp under
`<dimension-root>/.world-tier/access/` when shared region I/O begins. The first
touch and later touches at the write interval are atomically published and
synced; an unreadable access marker disables cold selection for that dimension.

The catalog seeds existing local regions as hot when no history exists. Only
trust filesystem atime after verifying that the mount updates it reliably.
`--trust-atime` applies to one `catalog` command; setting
`trust_filesystem_atime: true` in the private config makes `catalog`,
`candidates`, and the automatic scheduler's `plan` use verified atime history.
The default is false. Candidate listing applies the configured
`cold_age_days` (default 30) and excludes entries in `protected_regions`, for example
`["overworld/r.0.0", "nether/r.3.-2"]`:

```sh
fiesta-world-tier catalog --config /etc/fiesta-world-tier/config.json
fiesta-world-tier candidates --config /etc/fiesta-world-tier/config.json
fiesta-world-tier plan --config /etc/fiesta-world-tier/config.json
fiesta-world-tier storage --config /etc/fiesta-world-tier/config.json
```

The budget planner selects oldest cold local bundles until the configured
`local_world_data_budget_bytes` is approached. It reports selected bundle
sizes, unmet amount, and an advisory snapshot of Luna's current
objects/staging usage; it constrains its candidate plan by reported quota
headroom but does not start a live-region offload. Before measuring quota it
removes offload stages older than seven days, deleting a matching remote
duplicate only when no remote-only marker exists and the complete local bundle
remains. The snapshot is unlocked and can race another writer; each actual
upload rechecks exact quota under the remote writer lock. Planning initializes local access
metadata and reports whether verified filesystem atime was trusted. Each
actual offload still requires Paper's
region lease, a healthy tracker, and an old access timestamp. The companion
Paper patch includes an opt-in scheduler that can try multiple candidates per
pass; online mode is gated by a JVM property and rechecks Paper's lease before
each transfer. The staged path copies the bundle during a lease, releases the
lease for network upload, then rechecks cold age and file identity under a new
lease. A player can use that region during upload; if it changed or was
accessed, eviction is canceled. Player-facing latency still needs a Paper
integration measurement. The scheduler is not installed on Fiesta. Use
`--max-selected 1` for a bounded one-region plan. Run the unit tests
with:

`storage` reports the apparent size of Oracle's three world directories, Luna's
dedicated tier objects and staging directories, the combined byte count, and
the configured remote quota/headroom. It does not write to either side. Luna's
measurement is an unlocked advisory snapshot and may race an upload or restore;
do not use it alone as a hard quota check. It is a reporting command, not a
5-second Chunky guard integration.

```sh
python3 -m unittest discover -s tests -v
```

The OpenSSH adapter requires Python 3.10 or later and the local `ssh` and
`sftp` clients. Its remote account must have a pinned host key, a private key
readable only by its owner, and permission to run `cat`, `du`, `mkdir`,
`chmod`, `test`, `rmdir`, and `rm` within a dedicated directory under that
account's home. It does not use `sudo`. Keep the host, identity path, known-hosts path,
control socket, and runtime configuration outside this repository. When
enabled, SSH connection multiplexing uses a `%C` socket path in a private
directory (mode `0700`) to avoid a new network handshake for every metadata
operation.

## Proposed production design

See [DESIGN.md](DESIGN.md), the
[automatic hot/cold tiering roadmap](docs/automatic-tiering-roadmap.md), and
the companion [Paper storage integration notes](docs/paper-storage-integration.md).
No files from production should be offloaded until crash recovery, concurrent
reads/writes, player movement during upload, remote outage, quota exhaustion,
and rollback all pass on disposable copies.
