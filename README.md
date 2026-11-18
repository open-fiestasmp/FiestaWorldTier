# Fiesta World Tier

Experimental, fail-closed region-file tiering for a Paper-based Minecraft world.

This repository is a lab prototype. It is **not ready for a live world** and does
not modify Fiesta's server. A plugin alone cannot safely move live `.mca` files:
the server owns region-file handles and performs chunk reads and writes inside
its storage layer. Production use requires a Paper storage integration that can
lock a whole region bundle, wait for pending saves, restore a cold bundle before
opening it, and stop rather than generate a replacement if restore fails.

The prototype models the durable transfer state machine and tests it against
disposable worlds and a local object-store fixture. It never scans, copies, or
deletes the live Fiesta world.

The prototype is designed to support a configurable hard quota. A sample
deployment should cap world-region objects at 80,000,000,000 bytes and leave
additional capacity for proxy and system data. It does not implement SSH
transport or a Paper storage hook yet.

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
- Bound Luna storage with a hard byte quota. Keep credentials outside this
  repository and transfer only region data over SSH.
- Keep the remote copy until a verified local restore is complete. Recover
  interrupted transactions from the journal before accepting chunk I/O.

## Prototype

`src/fiesta_world_tier/` contains the manifest, local fixture store, journal,
and region bundle mover. It is intentionally not wired to Paper. Run its unit
tests with:

```sh
python3 -m unittest discover -s tests -v
```

## Proposed production design

See [DESIGN.md](DESIGN.md). The next milestone is a Paper 1.21.11 storage-layer
patch and a disposable integration server. No files from production should be
offloaded until crash recovery, concurrent reads/writes, player movement into a
cold region, remote outage, quota exhaustion, and rollback all pass on copies.
