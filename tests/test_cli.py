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

from fiesta_world_tier.cli import _dimension_config, _read_marker, main
from fiesta_world_tier.leases import FileRegionLease
from fiesta_world_tier import RegionKey, TierError
from fiesta_world_tier.openssh_store import TransientSSHError


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

    def test_retryable_remote_transfer_errors_use_temporary_failure_exit_code(self):
        from argparse import Namespace
        from contextlib import redirect_stderr

        def fail(_args):
            raise TransientSSHError("SSH transport disconnected")

        stderr = io.StringIO()
        with patch("fiesta_world_tier.cli.build_parser") as build_parser:
            build_parser.return_value.parse_args.return_value = Namespace(handler=fail)
            with redirect_stderr(stderr):
                self.assertEqual(main([]), 75)
        self.assertIn("SSH transport disconnected", stderr.getvalue())

    def test_non_retryable_remote_transfer_errors_keep_failure_exit_code(self):
        from argparse import Namespace
        from contextlib import redirect_stderr

        def fail(_args):
            raise TransientSSHError("SSH operation timed out", retryable=False)

        stderr = io.StringIO()
        with patch("fiesta_world_tier.cli.build_parser") as build_parser:
            build_parser.return_value.parse_args.return_value = Namespace(handler=fail)
            with redirect_stderr(stderr):
                self.assertEqual(main([]), 1)
        self.assertIn("SSH operation timed out", stderr.getvalue())


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

    def test_staged_snapshot_and_finalize_require_a_live_paper_region_lock(self):
        from fiesta_world_tier.cli import build_parser, finalize_offload_snapshot, snapshot_offload
        from fiesta_world_tier.paper_lease import paper_lease_is_held

        class FakeManager:
            snapshot_args = None
            finalize_args = None

            def snapshot_offload(self, *args):
                self.snapshot_args = args

            def finalize_offload_snapshot(self, *args):
                self.finalize_args = args

        manager = FakeManager()
        snapshot_id = "12345678-1234-1234-1234-123456789abc"
        snapshot_args = build_parser().parse_args([
            "snapshot-offload", "--config", str(self.config),
            "--dimension-key", "minecraft:overworld", "--dimension-root", str(self.world),
            "--region-x", "-1", "--region-z", "2", "--snapshot-id", snapshot_id,
        ])
        with patch("fiesta_world_tier.cli._make_manager", return_value=manager):
            with self.assertRaisesRegex(TierError, "without Paper's exclusive region lease"):
                snapshot_offload(snapshot_args)
            self.assertIsNone(manager.snapshot_args)

            process = self._hold_paper_lease()
            try:
                self.assertTrue(paper_lease_is_held(self.world, self.key))
                snapshot_offload(snapshot_args)
                self.assertEqual(manager.snapshot_args[0], self.key)
                self.assertEqual(manager.snapshot_args[1], snapshot_id)
                self.assertEqual(manager.snapshot_args[2], 30 * 86_400)

                finalize_args = build_parser().parse_args([
                    "finalize-offload-snapshot", "--config", str(self.config),
                    "--dimension-key", "minecraft:overworld", "--dimension-root", str(self.world),
                    "--region-x", "-1", "--region-z", "2", "--snapshot-id", snapshot_id,
                    "--manifest-sha256", "a" * 64,
                ])
                finalize_offload_snapshot(finalize_args)
                self.assertEqual(manager.finalize_args[0], self.key)
                self.assertEqual(manager.finalize_args[1], snapshot_id)
                self.assertEqual(manager.finalize_args[2], "a" * 64)
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
                self.trust_atime = None

            def catalog_local_regions(self, trust_atime=False):
                self.catalog_calls += 1
                self.trust_atime = trust_atime

            def cleanup_stale_offload_snapshots(self):
                return 0
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
        self.assertFalse(manager.trust_atime)
        self.assertEqual(manager.age_seconds, 7 * 86_400)
        self.assertEqual(json.loads(output.getvalue()), [
            {"dimension": "overworld", "region_x": -1, "region_z": 2},
        ])

        protected_config = Path(self.temp.name) / "protected-config.json"
        payload = json.loads(self.config.read_text(encoding="utf-8"))
        payload["protected_regions"] = ["overworld/r.-1.2"]
        payload["trust_filesystem_atime"] = True
        protected_config.write_text(json.dumps(payload), encoding="utf-8")
        protected_config.chmod(0o600)
        args = build_parser().parse_args(["candidates", "--config", str(protected_config)])
        output = io.StringIO()
        with patch("fiesta_world_tier.cli._make_manager", return_value=manager):
            with redirect_stdout(output):
                candidates(args)
        self.assertEqual(json.loads(output.getvalue()), [])
        self.assertTrue(manager.trust_atime)

        payload["protected_regions"] = ["not-a-region"]
        protected_config.write_text(json.dumps(payload), encoding="utf-8")
        with patch("fiesta_world_tier.cli._make_manager", return_value=manager):
            with self.assertRaisesRegex(TierError, "protected_regions"):
                candidates(args)

    def test_plan_command_serializes_budget_selection_as_dry_run(self):
        from fiesta_world_tier.cli import build_parser, plan

        class FakeManager:
            def __init__(self):
                self.catalog_calls = 0
                self.trust_atime = None
                self.arguments = None
                self.store = type("FakeStore", (), {
                    "max_bytes": 10_000,
                    "minimum_free_bytes": 1000,
                    "usage_snapshot": lambda _self: 250,
                    "available_snapshot": lambda _self: 5000,
                })()

            def catalog_local_regions(self, trust_atime=False):
                self.catalog_calls += 1
                self.trust_atime = trust_atime

            def cleanup_stale_offload_snapshots(self):
                return 0

            def plan_local_budget(
                self, target_bytes, age_seconds, protected, remote_available_bytes, max_selected
            ):
                self.arguments = (target_bytes, age_seconds, protected, remote_available_bytes, max_selected)
                return {
                    "current_bytes": 2048,
                    "target_bytes": target_bytes,
                    "needed_bytes": 1024,
                    "planned_bytes": 1500,
                    "unmet_bytes": 0,
                    "selected": [{
                        "key": RegionKey("overworld", -1, 2),
                        "bytes": 1500,
                        "remote_bytes": 1600,
                        "last_access": 100.0,
                    }],
                }

        manager = FakeManager()
        args = build_parser().parse_args([
            "plan", "--config", str(self.config), "--target-bytes", "1024", "--max-selected", "1",
        ])
        output = io.StringIO()
        with patch("fiesta_world_tier.cli._make_manager", return_value=manager):
            with redirect_stdout(output):
                plan(args)

        payload = json.loads(output.getvalue())
        self.assertEqual(manager.catalog_calls, 1)
        self.assertFalse(manager.trust_atime)
        self.assertEqual(manager.arguments, (1024, 30 * 86_400, set(), 4000, 1))
        self.assertEqual(payload["selected"], [{
            "dimension": "overworld",
            "region_x": -1,
            "region_z": 2,
            "bytes": 1500,
            "remote_bytes": 1600,
            "last_access": 100.0,
        }])
        self.assertEqual(payload["mode"], "dry-run")
        self.assertFalse(payload["filesystem_atime_trusted"])
        self.assertFalse(payload["automatic_eviction_enabled"])
        self.assertTrue(payload["requires_paper_region_lease_before_each_offload"])
        self.assertEqual(payload["remote_usage_bytes"], 250)
        self.assertEqual(payload["remote_available_bytes"], 4000)
        self.assertEqual(payload["remote_quota_headroom_bytes"], 9750)
        self.assertEqual(payload["remote_filesystem_free_bytes"], 5000)
        self.assertEqual(payload["remote_minimum_free_bytes"], 1000)
        self.assertEqual(payload["remote_free_headroom_bytes"], 4000)
        self.assertTrue(payload["remote_usage_snapshot_is_advisory"])
        self.assertTrue(payload["remote_free_snapshot_is_advisory"])

        config_payload = json.loads(self.config.read_text(encoding="utf-8"))
        config_payload["trust_filesystem_atime"] = True
        self.config.write_text(json.dumps(config_payload), encoding="utf-8")
        self.config.chmod(0o600)
        manager = FakeManager()
        args = build_parser().parse_args([
            "plan", "--config", str(self.config), "--target-bytes", "1024",
        ])
        output = io.StringIO()
        with patch("fiesta_world_tier.cli._make_manager", return_value=manager):
            with redirect_stdout(output):
                plan(args)
        payload = json.loads(output.getvalue())
        self.assertTrue(manager.trust_atime)
        self.assertTrue(payload["filesystem_atime_trusted"])

    def test_plan_rejects_non_boolean_filesystem_atime_setting(self):
        from fiesta_world_tier.cli import build_parser, plan

        payload = json.loads(self.config.read_text(encoding="utf-8"))
        payload["trust_filesystem_atime"] = "yes"
        self.config.write_text(json.dumps(payload), encoding="utf-8")
        self.config.chmod(0o600)
        args = build_parser().parse_args([
            "plan", "--config", str(self.config), "--target-bytes", "1024",
        ])
        with patch("fiesta_world_tier.cli._make_manager") as make_manager:
            with self.assertRaisesRegex(TierError, "trust_filesystem_atime must be a boolean"):
                plan(args)
        make_manager.assert_not_called()


class StorageCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server_root = Path(self.temp.name) / "server"
        self.server_root.mkdir()
        for name in ("world", "world_nether", "world_the_end"):
            root = self.server_root / name
            root.mkdir()
            (root / "region.dat").write_bytes(name.encode("ascii"))
        self.config = Path(self.temp.name) / "tier-config.json"
        self.config.write_text(json.dumps({"server_root": str(self.server_root)}), encoding="utf-8")
        self.config.chmod(0o600)

    def tearDown(self):
        self.temp.cleanup()

    def test_storage_command_reports_local_remote_and_combined_bytes_read_only(self):
        from fiesta_world_tier.cli import build_parser, storage

        class FakeStore:
            max_bytes = 1000
            minimum_free_bytes = 100

            @staticmethod
            def usage_snapshot():
                return 300

            @staticmethod
            def available_snapshot():
                return 900

        manager = type("FakeManager", (), {"store": FakeStore()})()
        args = build_parser().parse_args(["storage", "--config", str(self.config)])
        output = io.StringIO()
        with patch("fiesta_world_tier.cli._make_manager", return_value=manager):
            with redirect_stdout(output):
                storage(args)

        payload = json.loads(output.getvalue())
        self.assertGreaterEqual(payload["local_world_bytes"], sum(
            len(name.encode("ascii")) for name in ("world", "world_nether", "world_the_end")
        ))
        self.assertEqual(payload["remote_tier_store_bytes"], 300)
        self.assertEqual(
            payload["combined_world_storage_bytes"], payload["local_world_bytes"] + 300
        )
        self.assertEqual(payload["remote_quota_bytes"], 1000)
        self.assertEqual(payload["remote_available_bytes"], 700)
        self.assertEqual(payload["remote_filesystem_free_bytes"], 900)
        self.assertEqual(payload["remote_minimum_free_bytes"], 100)
        self.assertEqual(payload["remote_free_headroom_bytes"], 800)
        self.assertTrue(payload["remote_free_snapshot_is_advisory"])
        self.assertTrue(payload["remote_usage_includes_staging"])
        self.assertTrue(payload["remote_usage_snapshot_is_advisory"])
        self.assertFalse(payload["mutated_world_or_remote_data"])

    def test_storage_command_fails_closed_for_missing_world_root(self):
        from fiesta_world_tier.cli import build_parser, storage

        end_root = self.server_root / "world_the_end"
        (end_root / "region.dat").unlink()
        end_root.rmdir()
        args = build_parser().parse_args(["storage", "--config", str(self.config)])
        with self.assertRaisesRegex(TierError, "missing or unsafe"):
            storage(args)


if __name__ == "__main__":
    unittest.main()
