from __future__ import annotations

import argparse
import json
import re
import stat
import sys
from pathlib import Path, PurePosixPath

from .leases import FileRegionLease
from .openssh_store import OpenSSHConfig, OpenSSHObjectStore
from .store import DIMENSION_KEYS, DIMENSION_ROOTS, RegionKey, TierError, TierManager


def _private_json(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise TierError("configuration must be a regular non-symlink file")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise TierError("configuration permissions must exclude group and other access")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TierError("configuration could not be read as JSON") from exc
    if not isinstance(value, dict):
        raise TierError("configuration root must be an object")
    return value


def _dimension_config(config: dict, dimension_key: str, passed_root: Path) -> tuple[str, Path]:
    logical_dimension = {minecraft_key: name for name, minecraft_key in DIMENSION_KEYS.items()}.get(dimension_key)
    if logical_dimension is None:
        raise TierError("unsupported Minecraft dimension key")
    dimensions = config.get("dimensions")
    if not isinstance(dimensions, dict):
        raise TierError("configuration dimensions must be an object")
    relative = dimensions.get(dimension_key)
    if not isinstance(relative, str):
        raise TierError("configuration has no root for this Minecraft dimension")
    relative_path = PurePosixPath(relative)
    if relative_path.is_absolute() or any(part in ("", ".", "..") for part in relative_path.parts):
        raise TierError("configured dimension root must be a clean relative path")
    server_root = Path(config["server_root"]).resolve(strict=True)
    expected_root = (server_root / Path(*relative_path.parts)).resolve(strict=True)
    actual_root = passed_root.resolve(strict=True)
    if not expected_root.is_relative_to(server_root) or actual_root != expected_root:
        raise TierError("Paper dimension root does not match the configured server world")
    if not actual_root.is_dir() or actual_root.is_symlink():
        raise TierError("configured dimension root is not a safe directory")
    if str(DIMENSION_ROOTS[logical_dimension]) != relative:
        raise TierError("dimension mapping does not match the supported Fiesta region layout")
    return logical_dimension, actual_root


def _read_marker(dimension_root: Path, key: RegionKey, dimension_key: str) -> str:
    marker = dimension_root / ".world-tier" / "remote" / f"r.{key.x}.{key.z}.json"
    if marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 4096:
        raise TierError("cold-region marker is missing or invalid")
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TierError("cold-region marker is not valid JSON") from exc
    if (not isinstance(payload, dict) or payload.get("format") != 1
            or payload.get("dimension") != key.dimension
            or payload.get("dimension_key") != dimension_key
            or payload.get("state") not in ("REMOTE_ONLY", "RESTORING")
            or type(payload.get("x")) is not int or payload["x"] != key.x
            or type(payload.get("z")) is not int or payload["z"] != key.z
            or not re.fullmatch(r"[0-9a-f]{64}", str(payload.get("manifest_sha256", "")))):
        raise TierError("cold-region marker identity or checksum is invalid")
    return payload["manifest_sha256"]


def restore(args: argparse.Namespace) -> None:
    config_path = Path(args.config).absolute()
    config = _private_json(config_path)
    logical_dimension, dimension_root = _dimension_config(
        config, args.dimension_key, Path(args.dimension_root)
    )
    key = RegionKey(logical_dimension, args.region_x, args.region_z)
    expected_hash = _read_marker(dimension_root, key, args.dimension_key)
    remote = config.get("remote")
    if not isinstance(remote, dict):
        raise TierError("configuration remote storage must be an object")
    store_config = OpenSSHConfig(
        host=remote["host"],
        username=remote["username"],
        port=int(remote["port"]),
        identity_file=Path(remote["identity_file"]),
        known_hosts=Path(remote["known_hosts"]),
        remote_root=PurePosixPath(remote["remote_root"]),
        max_bytes=int(remote["max_bytes"]),
        timeout_seconds=int(remote.get("timeout_seconds", 900)),
    )
    store = OpenSSHObjectStore(store_config)
    server_root = Path(config["server_root"]).resolve(strict=True)
    lease = FileRegionLease(server_root)
    manager = TierManager(
        server_root,
        Path(config["journal"]),
        store,
        lease.acquire,
        local_free_reserve_bytes=int(config.get("local_free_reserve_bytes", 15 * 1024**3)),
    )
    manager.restore(key, expected_manifest_hash=expected_hash)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fiesta-world-tier")
    subparsers = parser.add_subparsers(dest="operation", required=True)
    restore_parser = subparsers.add_parser("restore", help="restore a marked remote-only region")
    restore_parser.add_argument("--config", required=True)
    restore_parser.add_argument("--dimension-key", required=True)
    restore_parser.add_argument("--dimension-root", required=True)
    restore_parser.add_argument("--region-x", required=True, type=int)
    restore_parser.add_argument("--region-z", required=True, type=int)
    restore_parser.set_defaults(handler=restore)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.handler(args)
    except (KeyError, OSError, TypeError, ValueError, TierError) as exc:
        print(f"fiesta-world-tier: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
