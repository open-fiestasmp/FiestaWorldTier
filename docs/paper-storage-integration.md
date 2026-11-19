# Paper restore and offload integration

The companion Paper 1.21.11 patch adds a narrow restore seam to
`RegionFileStorage`. When a terrain region is absent locally and has a marker
at `<dimension-root>/.world-tier/remote/r.<x>.<z>.json`, Paper calls the
`fiesta-world-tier restore` command before returning no data or creating a new
region. It also checks this marker before trusting Moonrise's negative cache.
The child receives an argument vector (no shell), a minimal environment, a
bounded timeout, and a private configuration path. A failed restore propagates
as an I/O error; it cannot silently become an empty world region.

Patch `0038` also exposes the experimental
`MoonriseRegionFileIO.offloadRegion(world, regionX, regionZ)` API. Paper checks
the region on its main thread, closes cached terrain/entity/POI files, then
holds the exclusive region lease while a worker runs
`fiesta-world-tier offload`. The command refuses to proceed unless it can
verify Paper's live cross-process file lock. This Java/Python lock handoff has
been checked on Linux.

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
It does not move files or measure remote quota. Cataloging and planning do
persist local access metadata, but do not offload regions. Set `cold_age_days`
in the private JSON config; candidate selection and the offload command use
that value, and offload rechecks age under Paper's exclusive lock.

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
treated as new terrain. The Paper patch includes an explicit leased offload
API and the Python `offload` command, which requires Paper's held OS file lock
and rechecks the configured age under that lock.
There is an experimental access-age catalog, dry-run candidate listing, and
dry-run local-budget planner, but no remote-headroom-aware controller or
automatic scheduler, and no production caller invokes the API. Paper patch
`0039` refuses offload after an access-tracking failure. Do not enable
offloading on a live world until budget selection, restart/crash recovery, and
concurrency behavior pass disposable-server tests. The patch has not been
installed on Fiesta or exercised against a disposable Paper world.

The integration is intentionally fail-closed. If the marker, command,
configuration, SSH host key, remote bundle, checksum, quota, or local disk
reserve check fails, the operation returns an I/O error and leaves the world
files untouched. Keep a separate complete backup before any production rollout.
