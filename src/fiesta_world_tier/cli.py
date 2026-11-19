from __future__ import annotations

import argparse
import json
import math
import re
import stat
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath

from .leases import FileRegionLease
from .openssh_store import OpenSSHConfig, OpenSSHObjectStore
from .paper_lease import paper_lease_is_held
from .store import DIMENSION_KEYS, DIMENSION_ROOTS, RegionKey, TierError, TierManager


WORLD_STORAGE_ROOTS = ("world", "world_nether", "world_the_end")


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
            or payload.get("state") not in ("REMOTE_ONLY", "EVICTING", "RESTORING")
            or type(payload.get("x")) is not int or payload["x"] != key.x
            or type(payload.get("z")) is not int or payload["z"] != key.z
            or not re.fullmatch(r"[0-9a-f]{64}", str(payload.get("manifest_sha256", "")))):
        raise TierError("cold-region marker identity or checksum is invalid")
    return payload["manifest_sha256"]


def _make_manager(config: dict) -> TierManager:
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
        control_path=Path(remote["control_path"]) if remote.get("control_path") else None,
        compression=remote.get("compression", False),
        timeout_seconds=int(remote.get("timeout_seconds", 900)),
    )
    store = OpenSSHObjectStore(store_config)
    server_root = Path(config["server_root"]).resolve(strict=True)
    lease = FileRegionLease(server_root)
    return TierManager(
        server_root,
        Path(config["journal"]),
        store,
        lease.acquire,
        local_free_reserve_bytes=int(config.get("local_free_reserve_bytes", 15 * 1024**3)),
    )


def _protected_region_names(config: dict) -> set[str]:
    protected = config.get("protected_regions", [])
    if not isinstance(protected, list) or any(
        not isinstance(name, str)
        or not re.fullmatch(r"(?:overworld|nether|end)/r\.-?\d+\.-?\d+", name)
        for name in protected
    ):
        raise TierError("protected_regions must be a list of dimension/r.x.z region names")
    return set(protected)


def _trust_filesystem_atime(config: dict) -> bool:
    value = config.get("trust_filesystem_atime", False)
    if type(value) is not bool:
        raise TierError("trust_filesystem_atime must be a boolean")
    return value


def offload(args: argparse.Namespace) -> None:
    config = _private_json(Path(args.config).absolute())
    logical_dimension, dimension_root = _dimension_config(
        config, args.dimension_key, Path(args.dimension_root)
    )
    key = RegionKey(logical_dimension, args.region_x, args.region_z)
    age_days = config.get("cold_age_days", 30)
    if not isinstance(age_days, (int, float)) or isinstance(age_days, bool) or not math.isfinite(age_days) or age_days <= 0:
        raise TierError("cold age must be a positive finite number of days")
    if key.name in _protected_region_names(config):
        raise TierError(f"refusing offload of protected region: {key.name}")
    if not paper_lease_is_held(dimension_root, key):
        raise TierError("refusing offload without Paper's exclusive region lease")
    manager = _make_manager(config)
    last_access = manager.last_access(key)
    if last_access is None or time.time() - last_access < float(age_days) * 86_400:
        raise TierError("refusing offload because region access history is recent or unknown")
    manager.offload(key)


def _snapshot_key(config: dict, dimension_key: str, region_x: int, region_z: int) -> RegionKey:
    logical_dimension = {minecraft_key: name for name, minecraft_key in DIMENSION_KEYS.items()}.get(dimension_key)
    if logical_dimension is None:
        raise TierError("unsupported Minecraft dimension key")
    if f"{logical_dimension}/r.{region_x}.{region_z}" in _protected_region_names(config):
        raise TierError("refusing tier operation for a protected region")
    return RegionKey(logical_dimension, region_x, region_z)


def _cold_age_seconds(config: dict) -> float:
    age_days = config.get("cold_age_days", 30)
    if (not isinstance(age_days, (int, float)) or isinstance(age_days, bool)
            or not math.isfinite(age_days) or age_days <= 0):
        raise TierError("cold age must be a positive finite number of days")
    return float(age_days) * 86_400


def snapshot_offload(args: argparse.Namespace) -> None:
    config = _private_json(Path(args.config).absolute())
    logical_dimension, dimension_root = _dimension_config(
        config, args.dimension_key, Path(args.dimension_root)
    )
    key = RegionKey(logical_dimension, args.region_x, args.region_z)
    if key.name in _protected_region_names(config):
        raise TierError(f"refusing offload of protected region: {key.name}")
    if not paper_lease_is_held(dimension_root, key):
        raise TierError("refusing to snapshot without Paper's exclusive region lease")
    _make_manager(config).snapshot_offload(key, args.snapshot_id, _cold_age_seconds(config))


def publish_offload_snapshot(args: argparse.Namespace) -> None:
    config = _private_json(Path(args.config).absolute())
    key = _snapshot_key(config, args.dimension_key, args.region_x, args.region_z)
    manifest_hash = _make_manager(config).publish_offload_snapshot(key, args.snapshot_id)
    print(manifest_hash)


def finalize_offload_snapshot(args: argparse.Namespace) -> None:
    config = _private_json(Path(args.config).absolute())
    logical_dimension, dimension_root = _dimension_config(
        config, args.dimension_key, Path(args.dimension_root)
    )
    key = RegionKey(logical_dimension, args.region_x, args.region_z)
    if key.name in _protected_region_names(config):
        raise TierError(f"refusing offload of protected region: {key.name}")
    if not paper_lease_is_held(dimension_root, key):
        raise TierError("refusing finalization without Paper's exclusive region lease")
    _make_manager(config).finalize_offload_snapshot(
        key, args.snapshot_id, args.manifest_sha256, _cold_age_seconds(config)
    )


def abort_offload_snapshot(args: argparse.Namespace) -> None:
    config = _private_json(Path(args.config).absolute())
    key = _snapshot_key(config, args.dimension_key, args.region_x, args.region_z)
    _make_manager(config).abort_offload_snapshot(key, args.snapshot_id, args.manifest_sha256)


def restore(args: argparse.Namespace) -> None:
    config = _private_json(Path(args.config).absolute())
    logical_dimension, dimension_root = _dimension_config(
        config, args.dimension_key, Path(args.dimension_root)
    )
    key = RegionKey(logical_dimension, args.region_x, args.region_z)
    expected_hash = _read_marker(dimension_root, key, args.dimension_key)
    _make_manager(config).restore(key, expected_manifest_hash=expected_hash)


def catalog(args: argparse.Namespace) -> None:
    config = _private_json(Path(args.config).absolute())
    trust_atime = args.trust_atime or _trust_filesystem_atime(config)
    manager = _make_manager(config)
    count = manager.catalog_local_regions(trust_atime=trust_atime)
    print(json.dumps({"cataloged_local_regions": count}, separators=(",", ":")))


def storage(args: argparse.Namespace) -> None:
    """Report local world bytes and remote tier-store usage without changing either."""
    config = _private_json(Path(args.config).absolute())
    configured_root = Path(config["server_root"])
    if configured_root.is_symlink():
        raise TierError("configured server root must not be a symlink")
    server_root = configured_root.resolve(strict=True)
    if not server_root.is_dir():
        raise TierError("configured server root is not a safe directory")

    world_roots = [server_root / relative for relative in WORLD_STORAGE_ROOTS]
    for root in world_roots:
        if root.is_symlink() or not root.is_dir():
            raise TierError(f"configured world storage root is missing or unsafe: {root.name}")
    result = subprocess.run(
        ["du", "-sb", "--", *(str(root) for root in world_roots)],
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    if result.returncode != 0:
        raise TierError("could not measure local world storage")
    try:
        measurements = [int(line.split(maxsplit=1)[0]) for line in result.stdout.splitlines()]
    except (IndexError, ValueError) as exc:
        raise TierError("local world storage measurement is invalid") from exc
    if len(measurements) != len(world_roots):
        raise TierError("local world storage measurement is incomplete")
    local_world_bytes = sum(measurements)

    store = _make_manager(config).store
    remote_bytes = store.usage_snapshot()
    remote_quota_bytes = store.max_bytes
    print(json.dumps({
        "local_world_bytes": local_world_bytes,
        "remote_tier_store_bytes": remote_bytes,
        "combined_world_storage_bytes": local_world_bytes + remote_bytes,
        "remote_quota_bytes": remote_quota_bytes,
        "remote_available_bytes": max(0, remote_quota_bytes - remote_bytes),
        "remote_usage_includes_staging": True,
        "remote_usage_snapshot_is_advisory": True,
        "mutated_world_or_remote_data": False,
    }, separators=(",", ":")))


def candidates(args: argparse.Namespace) -> None:
    config = _private_json(Path(args.config).absolute())
    age_days = config.get("cold_age_days", 30) if args.age_days is None else args.age_days
    if not isinstance(age_days, (int, float)) or isinstance(age_days, bool) or not math.isfinite(age_days) or age_days <= 0:
        raise TierError("cold age must be a positive finite number of days")
    protected_names = _protected_region_names(config)
    trust_atime = _trust_filesystem_atime(config)
    manager = _make_manager(config)
    manager.catalog_local_regions(trust_atime=trust_atime)
    selected = manager.cold_regions(float(age_days) * 86_400)
    print(json.dumps([
        {"dimension": key.dimension, "region_x": key.x, "region_z": key.z}
        for key in selected
        if key.name not in protected_names
    ], separators=(",", ":")))


def plan(args: argparse.Namespace) -> None:
    """Print a size-budget selection without invoking Paper or moving data."""
    config = _private_json(Path(args.config).absolute())
    target_bytes = args.target_bytes
    if target_bytes is None:
        target_bytes = config.get("local_world_data_budget_bytes")
    if type(target_bytes) is not int or target_bytes < 0:
        raise TierError("set local_world_data_budget_bytes or pass --target-bytes as a non-negative integer")
    age_days = config.get("cold_age_days", 30)
    if (not isinstance(age_days, (int, float)) or isinstance(age_days, bool)
            or not math.isfinite(age_days) or age_days <= 0):
        raise TierError("cold age must be a positive finite number of days")
    protected_names = _protected_region_names(config)
    trust_atime = _trust_filesystem_atime(config)
    manager = _make_manager(config)
    manager.catalog_local_regions(trust_atime=trust_atime)
    stale_snapshots_cleaned = manager.cleanup_stale_offload_snapshots()
    remote_usage_bytes = manager.store.usage_snapshot()
    remote_quota_bytes = manager.store.max_bytes
    remote_available_bytes = max(0, remote_quota_bytes - remote_usage_bytes)
    result = manager.plan_local_budget(
        target_bytes,
        float(age_days) * 86_400,
        protected_names,
        remote_available_bytes=remote_available_bytes,
        max_selected=args.max_selected,
    )
    result["selected"] = [
        {
            "dimension": item["key"].dimension,
            "region_x": item["key"].x,
            "region_z": item["key"].z,
            "bytes": item["bytes"],
            "remote_bytes": item["remote_bytes"],
            "last_access": item["last_access"],
        }
        for item in result["selected"]
    ]
    result.update({
        "mode": "dry-run",
        "cold_age_days": age_days,
        "filesystem_atime_trusted": trust_atime,
        "protected_regions": sorted(protected_names),
        "remote_usage_bytes": remote_usage_bytes,
        "remote_quota_bytes": remote_quota_bytes,
        "remote_available_bytes": remote_available_bytes,
        "remote_usage_snapshot_is_advisory": True,
        "requires_paper_region_lease_before_each_offload": True,
        "stale_offload_snapshots_cleaned": stale_snapshots_cleaned,
        "automatic_eviction_enabled": False,
    })
    print(json.dumps(result, separators=(",", ":")))


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
    offload_parser = subparsers.add_parser(
        "offload", help="offload a region while Paper holds its exclusive lease"
    )
    offload_parser.add_argument("--config", required=True)
    offload_parser.add_argument("--dimension-key", required=True)
    offload_parser.add_argument("--dimension-root", required=True)
    offload_parser.add_argument("--region-x", required=True, type=int)
    offload_parser.add_argument("--region-z", required=True, type=int)
    offload_parser.set_defaults(handler=offload)
    snapshot_parser = subparsers.add_parser(
        "snapshot-offload", help="copy a cold region under Paper's lease before asynchronous upload"
    )
    snapshot_parser.add_argument("--config", required=True)
    snapshot_parser.add_argument("--dimension-key", required=True)
    snapshot_parser.add_argument("--dimension-root", required=True)
    snapshot_parser.add_argument("--region-x", required=True, type=int)
    snapshot_parser.add_argument("--region-z", required=True, type=int)
    snapshot_parser.add_argument("--snapshot-id", required=True)
    snapshot_parser.set_defaults(handler=snapshot_offload)
    publish_snapshot_parser = subparsers.add_parser(
        "publish-offload-snapshot", help="upload a private immutable region snapshot without a Paper lease"
    )
    publish_snapshot_parser.add_argument("--config", required=True)
    publish_snapshot_parser.add_argument("--dimension-key", required=True)
    publish_snapshot_parser.add_argument("--region-x", required=True, type=int)
    publish_snapshot_parser.add_argument("--region-z", required=True, type=int)
    publish_snapshot_parser.add_argument("--snapshot-id", required=True)
    publish_snapshot_parser.set_defaults(handler=publish_offload_snapshot)
    finalize_parser = subparsers.add_parser(
        "finalize-offload-snapshot", help="evict a staged region after rechecking Paper's lease and source files"
    )
    finalize_parser.add_argument("--config", required=True)
    finalize_parser.add_argument("--dimension-key", required=True)
    finalize_parser.add_argument("--dimension-root", required=True)
    finalize_parser.add_argument("--region-x", required=True, type=int)
    finalize_parser.add_argument("--region-z", required=True, type=int)
    finalize_parser.add_argument("--snapshot-id", required=True)
    finalize_parser.add_argument("--manifest-sha256", required=True)
    finalize_parser.set_defaults(handler=finalize_offload_snapshot)
    abort_parser = subparsers.add_parser(
        "abort-offload-snapshot", help="discard an uncommitted staged transfer, preserving any marked remote region"
    )
    abort_parser.add_argument("--config", required=True)
    abort_parser.add_argument("--dimension-key", required=True)
    abort_parser.add_argument("--region-x", required=True, type=int)
    abort_parser.add_argument("--region-z", required=True, type=int)
    abort_parser.add_argument("--snapshot-id", required=True)
    abort_parser.add_argument("--manifest-sha256")
    abort_parser.set_defaults(handler=abort_offload_snapshot)
    catalog_parser = subparsers.add_parser(
        "catalog", help="record local regions and initialize access ages"
    )
    catalog_parser.add_argument("--config", required=True)
    catalog_parser.add_argument(
        "--trust-atime", action="store_true",
        help="use filesystem access times only when the mount has verified atime semantics",
    )
    catalog_parser.set_defaults(handler=catalog)
    storage_parser = subparsers.add_parser(
        "storage", help="report local world bytes and remote tier-store usage"
    )
    storage_parser.add_argument("--config", required=True)
    storage_parser.set_defaults(handler=storage)
    candidates_parser = subparsers.add_parser(
        "candidates", help="print regions older than the configured cold-age threshold"
    )
    candidates_parser.add_argument("--config", required=True)
    candidates_parser.add_argument("--age-days", type=float)
    candidates_parser.set_defaults(handler=candidates)
    plan_parser = subparsers.add_parser(
        "plan", help="dry-run a cold-region selection to approach the local world-data budget"
    )
    plan_parser.add_argument("--config", required=True)
    plan_parser.add_argument("--target-bytes", type=int)
    plan_parser.add_argument(
        "--max-selected", type=int,
        help="limit the dry-run result to this many oldest cold bundles",
    )
    plan_parser.set_defaults(handler=plan)
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
