from __future__ import annotations

import json
import io
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

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
            "cold_age_days": 30,
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

    def test_manager_factory_returns_a_tier_manager(self):
        from fiesta_world_tier.cli import _make_manager

        config = {
            "server_root": str(self.server_root),
            "journal": str(Path(self.temp.name) / "manager.sqlite3"),
            "remote": {
                "host": "storage.example.invalid",
                "username": "fiesta",
                "port": 22,
                "identity_file": str(Path(self.temp.name) / "identity"),
                "known_hosts": str(Path(self.temp.name) / "known_hosts"),
                "remote_root": "/fiesta-world",
                "max_bytes": 1024,
            },
        }
        store = object()
        with patch("fiesta_world_tier.cli.OpenSSHObjectStore", return_value=store):
            manager = _make_manager(config)

        self.assertEqual(manager.server_root, self.server_root.resolve())
        self.assertIs(manager.store, store)
        self.assertEqual(manager.local_free_reserve_bytes, 15 * 1024**3)

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

    def test_offload_cli_refuses_configured_protected_region(self):
        from fiesta_world_tier.cli import build_parser, offload

        payload = json.loads(self.config.read_text(encoding="utf-8"))
        payload["protected_regions"] = ["overworld/r.-1.2"]
        self.config.write_text(json.dumps(payload), encoding="utf-8")
        args = build_parser().parse_args([
            "offload", "--config", str(self.config),
            "--dimension-key", "minecraft:overworld",
            "--dimension-root", str(self.world), "--region-x", "-1", "--region-z", "2",
        ])
        with self.assertRaisesRegex(TierError, "protected region"):
            offload(args)
        self.assertTrue(self.region.is_file())

    def test_offload_cli_refuses_recent_access_even_with_paper_lock(self):
        from fiesta_world_tier.cli import build_parser, offload

        class FakeManager:
            offload_called = False

            def last_access(self, _key):
                return time.time() - 60

            def offload(self, _key):
                self.offload_called = True

        manager = FakeManager()
        args = build_parser().parse_args([
            "offload", "--config", str(self.config),
            "--dimension-key", "minecraft:overworld",
            "--dimension-root", str(self.world), "--region-x", "-1", "--region-z", "2",
        ])
        process = self._hold_paper_lease()
        try:
            with patch("fiesta_world_tier.cli._make_manager", return_value=manager):
                with self.assertRaisesRegex(TierError, "recent or unknown"):
                    offload(args)
            self.assertFalse(manager.offload_called)
            self.assertTrue(self.region.is_file())
        finally:
            process.stdin.write("release\n")
            process.stdin.flush()
            process.wait(timeout=5)
            process.stdin.close()
            process.stdout.close()

    def test_candidates_command_catalogs_and_prints_age_eligible_regions(self):
        from fiesta_world_tier.cli import build_parser, candidates

        class FakeManager:
            def __init__(self):
                self.catalog_calls = 0
                self.age_seconds = None

            def catalog_local_regions(self):
                self.catalog_calls += 1
                return 1

            def cold_regions(self, age_seconds):
                self.age_seconds = age_seconds
                return [RegionKey("overworld", -1, 2)]

        manager = FakeManager()
        args = build_parser().parse_args([
            "candidates", "--config", str(self.config), "--age-days", "7",
        ])
        output = io.StringIO()
        with patch("fiesta_world_tier.cli._make_manager", return_value=manager):
            with redirect_stdout(output):
                candidates(args)

        self.assertEqual(manager.catalog_calls, 1)
        self.assertEqual(manager.age_seconds, 7 * 86_400)
        self.assertEqual(json.loads(output.getvalue()), [
            {"dimension": "overworld", "region_x": -1, "region_z": 2},
        ])

        protected_config = Path(self.temp.name) / "protected-config.json"
        payload = json.loads(self.config.read_text(encoding="utf-8"))
        payload["protected_regions"] = ["overworld/r.-1.2"]
        protected_config.write_text(json.dumps(payload), encoding="utf-8")
        protected_config.chmod(0o600)
        args = build_parser().parse_args(["candidates", "--config", str(protected_config)])
        output = io.StringIO()
        with patch("fiesta_world_tier.cli._make_manager", return_value=manager):
            with redirect_stdout(output):
                candidates(args)
        self.assertEqual(json.loads(output.getvalue()), [])

        payload["protected_regions"] = ["not-a-region"]
        protected_config.write_text(json.dumps(payload), encoding="utf-8")
        with patch("fiesta_world_tier.cli._make_manager", return_value=manager):
            with self.assertRaisesRegex(TierError, "protected_regions"):
                candidates(args)


if __name__ == "__main__":
    unittest.main()
