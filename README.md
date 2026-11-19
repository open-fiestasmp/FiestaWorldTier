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
identity file. The automated transport tests use a local fake; a separate
synthetic publish/verify/fetch/delete smoke test has passed against Luna without
using world data. The project never scans, copies, or deletes the live Fiesta
world.

The OpenSSH adapter enforces a configured application byte cap across its
objects and staging directories; this is not an OS-level filesystem quota.
Use a server-side quota as a second limit where the host supports it. A sample
deployment caps region objects at 90,000,000,000 bytes and leaves additional
capacity for proxy and system data. The companion Paper 1.21.11 patch provides
marked demand restore, per-region I/O leases, and an experimental
`offloadRegion(...)` API that holds Paper's exclusive lease while the CLI
uploads and evicts one region. The CLI also requires proof of Paper's
cross-process lock; Java/Python lock interoperation was verified on Linux.
There is no automatic cold-region selector or scheduler, and this integration
has not been exercised on a disposable server. Do not offload a production
world until the access-age policy and integration gates below pass.

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
use `--trust-atime` if the filesystem mount's access-time behavior has been
verified. Candidate listing is a dry run, applies the configured `cold_age_days`
(default 30), and excludes entries in `protected_regions`, for example
`["overworld/r.0.0", "nether/r.3.-2"]`:

```sh
fiesta-world-tier catalog --config /etc/fiesta-world-tier/config.json
fiesta-world-tier candidates --config /etc/fiesta-world-tier/config.json
```

Paper tracks region access and rejects offloading if its tracker became
unhealthy. The Python child then rechecks the configured access age while
Paper's exclusive OS lock is held. No candidate scheduler currently calls this
API. Run the unit tests with:

```sh
python3 -m unittest discover -s tests -v
```

The OpenSSH adapter requires Python 3.10 or later and the local `ssh` and
`sftp` clients. Its remote account must have a pinned host key, a private key
readable only by its owner, and permission to run `du`, `mkdir`, `chmod`,
`test`, `rmdir`, and `rm` within a dedicated directory under that account's
home. It does not use `sudo`. Keep the host, identity path, known-hosts path,
control socket, and runtime configuration outside this repository. When
enabled, SSH connection multiplexing uses a `%C` socket path in a private
directory (mode `0700`) to avoid a new network handshake for every metadata
operation.

## Proposed production design

See [DESIGN.md](DESIGN.md), the
[automatic hot/cold tiering roadmap](docs/automatic-tiering-roadmap.md), and
the companion [Paper storage integration notes](docs/paper-storage-integration.md).
The next milestone is a disposable integration server, access-age tracking, and
an automatic tiering policy that uses the Paper API. No files from production
should be offloaded until crash recovery, concurrent reads/writes, player
movement into a cold region, remote outage, quota exhaustion, and rollback all
pass on copies.
