from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fiesta_world_tier.cli import _dimension_config, _read_marker
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
        marker.write_text(json.dumps({
            "format": 1,
            "state": "REMOTE_ONLY",
            "dimension": "overworld",
            "dimension_key": "minecraft:overworld",
            "x": -2,
            "z": 5,
            "manifest_sha256": digest,
        }), encoding="utf-8")
        key = RegionKey("overworld", -2, 5)
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


if __name__ == "__main__":
    unittest.main()
