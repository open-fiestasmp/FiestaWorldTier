from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fiesta_world_tier.cli import _dimension_config, _read_marker
from fiesta_world_tier.leases import FileRegionLease
from fiesta_world_tier import RegionKey, TierError


class RestoreCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server_root = Path(self.temp.name) / "server"
        self.world = self.server_root / "world"
        self.world.mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def test_dimension_root_must_match_the_configured_world(self):
        config = {
            "server_root": str(self.server_root),
            "dimensions": {"minecraft:overworld": "world"},
        }
        logical, actual = _dimension_config(config, "minecraft:overworld", self.world)
        self.assertEqual(logical, "overworld")
        self.assertEqual(actual, self.world)
        with self.assertRaisesRegex(TierError, "does not match"):
            _dimension_config(config, "minecraft:overworld", Path(self.temp.name))

    def test_marker_identity_and_hash_are_validated(self):
        marker_dir = self.world / ".world-tier" / "remote"
        marker_dir.mkdir(parents=True)
        marker = marker_dir / "r.-2.5.json"
        digest = "a" * 64
        marker_payload = {
            "format": 1,
            "state": "REMOTE_ONLY",
            "dimension": "overworld",
            "dimension_key": "minecraft:overworld",
            "x": -2,
            "z": 5,
            "manifest_sha256": digest,
        }
        key = RegionKey("overworld", -2, 5)
        for state in ("REMOTE_ONLY", "EVICTING", "RESTORING"):
            marker_payload["state"] = state
            marker.write_text(json.dumps(marker_payload), encoding="utf-8")
            with self.subTest(state=state):
                self.assertEqual(_read_marker(self.world, key, "minecraft:overworld"), digest)
        with self.assertRaisesRegex(TierError, "identity or checksum"):
            _read_marker(self.world, key, "minecraft:the_nether")

    def test_marker_symlinks_are_rejected(self):
        marker_dir = self.world / ".world-tier" / "remote"
        marker_dir.mkdir(parents=True)
        outside = Path(self.temp.name) / "outside.json"
        outside.write_text("{}", encoding="utf-8")
        (marker_dir / "r.0.0.json").symlink_to(outside)
        with self.assertRaisesRegex(TierError, "missing or invalid"):
            _read_marker(self.world, RegionKey("overworld", 0, 0), "minecraft:overworld")


class PaperLeaseCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server_root = Path(self.temp.name) / "server"
        self.world = self.server_root / "world"
        self.region_dir = self.world / "region"
        self.region_dir.mkdir(parents=True)
        self.key = RegionKey("overworld", -1, 2)
        self.region = self.region_dir / "r.-1.2.mca"
        self.region.write_bytes(b"synthetic region bytes")
        self.lease_path = self.world / ".world-tier" / "paper-leases" / "r.-1.2.lock"
        self.lease_path.parent.mkdir(parents=True)
        self.lease_path.touch(mode=0o600)
        self.config = Path(self.temp.name) / "tier-config.json"
        self.config.write_text(json.dumps({
            "server_root": str(self.server_root),
            "journal": str(Path(self.temp.name) / "state.sqlite3"),
            "dimensions": {"minecraft:overworld": "world"},
        }), encoding="utf-8")
        self.config.chmod(0o600)

    def tearDown(self):
        self.temp.cleanup()

    def _hold_paper_lease(self):
        import subprocess

        program = (
            "import fcntl, os, sys; fd=os.open(sys.argv[1], os.O_RDWR); "
            "fcntl.lockf(fd, fcntl.LOCK_EX); print('ready', flush=True); sys.stdin.readline()"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", program, str(self.lease_path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        )
        self.assertEqual(process.stdout.readline().strip(), "ready")
        return process

    def test_offload_cli_requires_the_live_paper_region_lock(self):
        from fiesta_world_tier.paper_lease import paper_lease_is_held

        self.assertFalse(paper_lease_is_held(self.world, self.key))
        process = self._hold_paper_lease()
        try:
            self.assertTrue(paper_lease_is_held(self.world, self.key))
        finally:
            process.stdin.write("release\n")
            process.stdin.flush()
            process.wait(timeout=5)
            process.stdin.close()
            process.stdout.close()
        self.assertFalse(paper_lease_is_held(self.world, self.key))

    def test_offload_cli_refuses_without_paper_lock_and_does_not_delete_local_data(self):
        from fiesta_world_tier.cli import build_parser, offload

        args = build_parser().parse_args([
            "offload", "--config", str(self.config),
            "--dimension-key", "minecraft:overworld",
            "--dimension-root", str(self.world), "--region-x", "-1", "--region-z", "2",
        ])
        with self.assertRaisesRegex(TierError, "without Paper's exclusive region lease"):
            offload(args)
        self.assertTrue(self.region.is_file())


if __name__ == "__main__":
    unittest.main()
