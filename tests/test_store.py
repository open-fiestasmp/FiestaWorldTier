from __future__ import annotations

import sys
import os
import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fiesta_world_tier import DirectoryObjectStore, RegionBundle, RegionKey, TierError, TierManager


class Lease:
    def __init__(self, allow: bool = True):
        self.allow = allow
        self.calls = 0

    @contextmanager
    def acquire(self, _key):
        self.calls += 1
        yield self.allow


class BrokenStore(DirectoryObjectStore):
    def publish(self, key, files):
        raise OSError("fixture upload interrupted")


class MutatingManifestStore(DirectoryObjectStore):
    def fetch(self, key, destination):
        manifest = super().fetch(key, destination)
        self._manifest_path(key).write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
        return manifest


class RegionTierTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "server"
        self.remote = Path(self.temp.name) / "remote"
        self.root.mkdir()
        self.key = RegionKey("overworld", -2, 5)
        self.files = RegionBundle(self.root, self.key).files
        self.lease = Lease()
        self.store = DirectoryObjectStore(self.remote, max_bytes=10_000_000)
        self.manager = TierManager(
            self.root,
            Path(self.temp.name) / "journal" / "tier.sqlite",
            self.store,
            self.lease.acquire,
            local_free_reserve_bytes=0,
        )
        self.create_bundle()

    def tearDown(self):
        self.temp.cleanup()

    def create_bundle(self):
        root = self.root / "world"
        for component, content in (
            ("region", b"terrain region bytes"),
            ("entities", b"entity region bytes"),
            ("poi", b"poi region bytes"),
        ):
            path = root / component / "r.-2.5.mca"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        (root / "region/c.-64.160.mcc").write_bytes(b"external chunk data")
        (root / "region/c.0.0.mcc").write_bytes(b"different region; leave local")

    def local_contents(self):
        return {
            component: path.read_bytes()
            for component, path in RegionBundle(self.root, self.key).files().items()
        }

    def test_offload_restore_round_trip(self):
        original = self.local_contents()
        self.assertIn("region/c.-64.160.mcc", original)
        self.assertNotIn("region/c.0.0.mcc", original)
        self.manager.record_access(self.key, when=100)
        self.assertEqual(self.manager.cold_regions(500, now=700), [self.key])
        self.manager.offload(self.key)
        self.assertEqual(self.manager.state(self.key), "REMOTE_ONLY")
        self.assertFalse((self.root / "world/region/r.-2.5.mca").exists())
        marker = self.root / "world/.world-tier/remote/r.-2.5.json"
        marker_payload = json.loads(marker.read_text(encoding="utf-8"))
        self.assertEqual(marker_payload["dimension_key"], "minecraft:overworld")
        self.assertEqual(marker_payload["manifest_sha256"], self.store.manifest_hash(self.key))
        self.assertGreater(self.store.usage(), 0)

        self.manager.restore(self.key)
        self.assertEqual(self.manager.state(self.key), "LOCAL_ONLY")
        self.assertEqual(self.local_contents(), original)
        self.assertEqual(self.store.usage(), 0)
        self.assertFalse(marker.exists())
        self.assertEqual(self.lease.calls, 2)

    def test_paper_restore_can_recover_from_marker_after_journal_loss(self):
        original = self.local_contents()
        self.manager.offload(self.key)
        marker = json.loads((self.root / "world/.world-tier/remote/r.-2.5.json").read_text(encoding="utf-8"))
        new_manager = TierManager(
            self.root,
            Path(self.temp.name) / "replacement-journal.sqlite",
            self.store,
            self.lease.acquire,
            local_free_reserve_bytes=0,
        )
        new_manager.restore(self.key, expected_manifest_hash=marker["manifest_sha256"])
        self.assertEqual(self.local_contents(), original)
        self.assertFalse((self.root / "world/.world-tier/remote/r.-2.5.json").exists())

    def test_catalog_seeds_existing_files_as_new_until_tracking_age_passes(self):
        self.assertEqual(self.manager.catalog_local_regions(when=100), 1)
        self.assertEqual(self.manager.cold_regions(500, now=599), [])
        self.assertEqual(self.manager.cold_regions(500, now=600), [self.key])

    def test_catalog_can_use_verified_atime_as_conservative_initial_history(self):
        for path in RegionBundle(self.root, self.key).files().values():
            os.utime(path, (100, 990))
        self.assertEqual(self.manager.catalog_local_regions(trust_atime=True), 1)
        self.assertEqual(self.manager.cold_regions(500, now=1000), [self.key])

    def test_catalog_imports_durable_paper_access_timestamp(self):
        self.manager._write_access_marker(self.key, 900)
        self.assertEqual(self.manager.catalog_local_regions(when=1000), 1)
        self.assertEqual(self.manager.cold_regions(200, now=1000), [])
        self.assertEqual(self.manager.cold_regions(99, now=1000), [self.key])

    def test_corrupt_access_marker_fails_closed(self):
        marker = self.manager._access_marker_path(self.key)
        marker.parent.mkdir(parents=True)
        marker.write_text("not-a-timestamp\n", encoding="ascii")
        with self.assertRaisesRegex(TierError, "invalid region access marker"):
            self.manager.catalog_local_regions(when=1000)

    def test_busy_region_is_never_evicted(self):
        self.lease.allow = False
        with self.assertRaisesRegex(TierError, "busy"):
            self.manager.offload(self.key)
        self.assertEqual(self.manager.state(self.key), "LOCAL_ONLY")
        self.assertTrue((self.root / "world/region/r.-2.5.mca").exists())
        self.assertEqual(self.store.usage(), 0)

    def test_quota_rejection_keeps_local_bundle(self):
        tiny_store = DirectoryObjectStore(Path(self.temp.name) / "tiny", max_bytes=1)
        manager = TierManager(
            self.root,
            Path(self.temp.name) / "tiny.sqlite",
            tiny_store,
            self.lease.acquire,
            local_free_reserve_bytes=0,
        )
        with self.assertRaisesRegex(TierError, "quota"):
            manager.offload(self.key)
        self.assertTrue((self.root / "world/region/r.-2.5.mca").exists())
        self.assertEqual(manager.state(self.key), "LOCAL_ONLY")

    def test_restore_respects_local_free_space_reserve(self):
        manager = TierManager(
            self.root,
            Path(self.temp.name) / "reserve.sqlite",
            self.store,
            self.lease.acquire,
            local_free_reserve_bytes=2**80,
        )
        manager.offload(self.key)
        with self.assertRaisesRegex(TierError, "insufficient local free space"):
            manager.restore(self.key)
        self.assertEqual(manager.state(self.key), "REMOTE_ONLY")
        self.assertFalse((self.root / "world/region/r.-2.5.mca").exists())

    def test_manifest_change_during_fetch_never_publishes_local_data(self):
        store = MutatingManifestStore(Path(self.temp.name) / "mutating", max_bytes=10_000_000)
        manager = TierManager(
            self.root,
            Path(self.temp.name) / "mutating.sqlite",
            store,
            self.lease.acquire,
            local_free_reserve_bytes=0,
        )
        manager.offload(self.key)
        with self.assertRaisesRegex(TierError, "remote manifest changed during restore"):
            manager.restore(self.key)
        self.assertFalse((self.root / "world/region/r.-2.5.mca").exists())
        self.assertGreater(store.usage(), 0)

    def test_failed_upload_keeps_local_bundle(self):
        broken = BrokenStore(Path(self.temp.name) / "broken", max_bytes=1000)
        manager = TierManager(
            self.root,
            Path(self.temp.name) / "broken.sqlite",
            broken,
            self.lease.acquire,
            local_free_reserve_bytes=0,
        )
        with self.assertRaisesRegex(OSError, "interrupted"):
            manager.offload(self.key)
        self.assertTrue((self.root / "world/region/r.-2.5.mca").exists())
        self.assertEqual(manager.state(self.key), "LOCAL_ONLY")

    def test_remote_corruption_fails_closed_without_publishing_local_files(self):
        self.manager.offload(self.key)
        remote_region = self.store._object_path(self.key) / "region/r.-2.5.mca"
        remote_region.write_bytes(b"X" * len(b"terrain region bytes"))
        with self.assertRaisesRegex(TierError, "checksum mismatch|verification failed"):
            self.manager.restore(self.key)
        self.assertEqual(self.manager.state(self.key), "RESTORING")
        self.assertFalse((self.root / "world/region/r.-2.5.mca").exists())

    def test_journal_manifest_hash_detects_manifest_tampering(self):
        self.manager.offload(self.key)
        manifest_path = self.store._manifest_path(self.key)
        manifest_path.write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(TierError, "manifest checksum mismatch"):
            self.manager.restore(self.key)
        self.assertFalse((self.root / "world/region/r.-2.5.mca").exists())

    def test_corrupt_existing_remote_object_never_replaces_a_good_local_copy(self):
        self.manager.offload(self.key)
        remote_region = self.store._object_path(self.key) / "region/r.-2.5.mca"
        remote_region.write_bytes(b"X" * len(b"terrain region bytes"))
        self.create_bundle()
        self.manager._set_state(self.key, "LOCAL_ONLY")

        with self.assertRaisesRegex(TierError, "checksum mismatch"):
            self.manager.offload(self.key)
        self.assertEqual(self.local_contents()["region/r.-2.5.mca"], b"terrain region bytes")

    def test_interrupted_eviction_is_healed_from_verified_remote_bundle(self):
        original = self.local_contents()
        self.manager.offload(self.key)
        self.manager._set_state(self.key, "EVICTING")
        marker = self.manager._marker_path(self.key)
        marker_payload = json.loads(marker.read_text(encoding="utf-8"))
        marker_payload["state"] = "EVICTING"
        marker.write_text(json.dumps(marker_payload), encoding="utf-8")
        (self.root / "world/region/r.-2.5.mca").write_bytes(b"partial local fragment")
        self.manager.recover(self.key)
        self.assertEqual(self.manager.state(self.key), "LOCAL_ONLY")
        self.assertEqual(self.local_contents(), original)

    def test_bundle_rejects_symlink_members(self):
        path = self.root / "world/entities/r.-2.5.mca"
        path.unlink()
        outside = Path(self.temp.name) / "outside.mca"
        outside.write_bytes(b"outside")
        path.symlink_to(outside)
        with self.assertRaisesRegex(TierError, "regular region file"):
            RegionBundle(self.root, self.key).files()

    def test_invalid_dimension_rejected(self):
        with self.assertRaises(ValueError):
            RegionKey("../outside", 0, 0)


if __name__ == "__main__":
    unittest.main()
