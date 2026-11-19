from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath
from typing import Callable, Iterator, Mapping, Protocol


DIMENSION_ROOTS = {
    "overworld": Path("world"),
    "nether": Path("world_nether/DIM-1"),
    "end": Path("world_the_end/DIM1"),
}
DIMENSION_KEYS = {
    "overworld": "minecraft:overworld",
    "nether": "minecraft:the_nether",
    "end": "minecraft:the_end",
}
COMPONENTS = ("region", "entities", "poi")


class TierError(RuntimeError):
    """A tier operation was rejected or could not safely finish."""


@dataclass(frozen=True, order=True)
class RegionKey:
    dimension: str
    x: int
    z: int

    def __post_init__(self) -> None:
        if self.dimension not in DIMENSION_ROOTS:
            raise ValueError(f"unsupported dimension: {self.dimension}")

    @property
    def name(self) -> str:
        return f"{self.dimension}/r.{self.x}.{self.z}"


@dataclass(frozen=True)
class RegionBundle:
    server_root: Path
    key: RegionKey

    @property
    def filename(self) -> str:
        return f"r.{self.key.x}.{self.key.z}.mca"

    def files(self) -> dict[str, Path]:
        server_root = self.server_root.resolve()
        root = server_root / DIMENSION_ROOTS[self.key.dimension]
        found: dict[str, Path] = {}
        for component in COMPONENTS:
            path = root / component / self.filename
            if path.is_symlink():
                raise TierError(f"not a regular region file: {path}")
            if path.exists():
                resolved = path.resolve()
                if path.is_symlink() or not path.is_file() or not resolved.is_relative_to(server_root):
                    raise TierError(f"not a regular region file: {path}")
                found[f"{component}/{self.filename}"] = path
        region_dir = root / "region"
        if region_dir.exists():
            for path in region_dir.iterdir():
                match = re.fullmatch(r"c\.(-?\d+)\.(-?\d+)\.mcc", path.name)
                if not match:
                    continue
                chunk_x, chunk_z = int(match.group(1)), int(match.group(2))
                if chunk_x // 32 != self.key.x or chunk_z // 32 != self.key.z:
                    continue
                resolved = path.resolve()
                if path.is_symlink() or not path.is_file() or not resolved.is_relative_to(server_root):
                    raise TierError(f"not a regular external chunk file: {path}")
                found[f"region/{path.name}"] = path
        if f"region/{self.filename}" not in found:
            raise TierError(f"terrain region is missing: {self.key.name}")
        return found


class BundleObjectStore(Protocol):
    max_bytes: int

    def publish(self, key: RegionKey, files: Mapping[str, Path]) -> tuple[dict, str]: ...
    def read_manifest(self, key: RegionKey) -> dict: ...
    def read_manifest_with_hash(self, key: RegionKey) -> tuple[dict, str]: ...
    def manifest_hash(self, key: RegionKey) -> str: ...
    def fetch(
        self,
        key: RegionKey,
        destination: Path,
        *,
        expected_manifest: dict | None = None,
        expected_manifest_hash: str | None = None,
    ) -> dict: ...
    def delete(self, key: RegionKey, *, expected_manifest_hash: str | None = None) -> None: ...

    @staticmethod
    def _valid_member(key: RegionKey, member: str) -> bool: ...

    @staticmethod
    def _fsync_dir(path: Path) -> None: ...


class DirectoryObjectStore:
    """Local object-store fixture; production SSH transport is not implemented."""

    def __init__(self, root: Path, max_bytes: int):
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.root = root.resolve()
        self.max_bytes = max_bytes
        self.objects = self.root / "objects"
        self.staging = self.root / ".staging"
        self.objects.mkdir(parents=True, exist_ok=True)
        self.staging.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _object_path(self, key: RegionKey) -> Path:
        return self.objects / key.dimension / f"r.{key.x}.{key.z}"

    def _manifest_path(self, key: RegionKey) -> Path:
        return self._object_path(key) / "manifest.json"

    @staticmethod
    def _valid_member(key: RegionKey, member: str) -> bool:
        path = PurePosixPath(member)
        if path.is_absolute() or len(path.parts) != 2 or ".." in path.parts or "\\" in member:
            return False
        component, filename = path.parts
        region_name = f"r.{key.x}.{key.z}.mca"
        if component in COMPONENTS and filename == region_name:
            return True
        if component != "region":
            return False
        match = re.fullmatch(r"c\.(-?\d+)\.(-?\d+)\.mcc", filename)
        return bool(match and int(match.group(1)) // 32 == key.x and int(match.group(2)) // 32 == key.z)

    @staticmethod
    def _hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for part in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(part)
        return digest.hexdigest()

    def usage(self) -> int:
        total = 0
        for base in (self.objects, self.staging):
            if base.exists():
                for path in base.rglob("*"):
                    if path.is_file() and not path.is_symlink():
                        total += path.stat().st_size
        return total

    def publish(self, key: RegionKey, files: Mapping[str, Path]) -> tuple[dict, str]:
        with self._lock:
            manifest_files: dict[str, dict[str, int | str]] = {}
            before: dict[str, tuple[int, int, int, int]] = {}
            for member, source in sorted(files.items()):
                if not self._valid_member(key, member) or source.is_symlink() or not source.is_file():
                    raise TierError(f"invalid bundle member: {source}")
                stat = source.stat()
                before[member] = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
                manifest_files[member] = {
                    "name": source.name,
                    "size": stat.st_size,
                    "sha256": self._hash(source),
                }
                after = source.stat()
                if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != before[member]:
                    raise TierError(f"region file changed while being read: {source}")

            if f"region/r.{key.x}.{key.z}.mca" not in manifest_files:
                raise TierError("bundle must contain terrain region data")
            generation = {
                "format": 1,
                "region": {"dimension": key.dimension, "x": key.x, "z": key.z},
                "files": manifest_files,
            }
            encoded = (json.dumps(generation, sort_keys=True, separators=(",", ":")) + "\n").encode()
            manifest_hash = hashlib.sha256(encoded).hexdigest()
            total = sum(int(info["size"]) for info in manifest_files.values()) + len(encoded)
            if self.usage() + total > self.max_bytes:
                raise TierError("remote storage quota would be exceeded")

            stage = Path(tempfile.mkdtemp(prefix="bundle-", dir=self.staging))
            try:
                for member, source in sorted(files.items()):
                    destination = stage / member
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with source.open("rb") as src, destination.open("xb") as dst:
                        shutil.copyfileobj(src, dst, length=1024 * 1024)
                        dst.flush()
                        os.fsync(dst.fileno())
                    if self._hash(destination) != manifest_files[member]["sha256"]:
                        raise TierError(f"copy verification failed for {member}")
                manifest_path = stage / "manifest.json"
                with manifest_path.open("xb") as output:
                    output.write(encoded)
                    output.flush()
                    os.fsync(output.fileno())
                self._fsync_dir(stage)

                final = self._object_path(key)
                final.parent.mkdir(parents=True, exist_ok=True)
                if final.exists():
                    existing = self.verify(key)
                    if existing == generation:
                        shutil.rmtree(stage)
                        return generation, manifest_hash
                    raise TierError(f"remote object already exists with different contents: {key.name}")
                os.replace(stage, final)
                self._fsync_dir(final.parent)
                return generation, manifest_hash
            except Exception:
                if stage.exists():
                    shutil.rmtree(stage)
                raise

    def read_manifest(self, key: RegionKey) -> dict:
        return self.read_manifest_with_hash(key)[0]

    def read_manifest_with_hash(self, key: RegionKey) -> tuple[dict, str]:
        try:
            encoded = self._manifest_path(key).read_bytes()
            manifest = json.loads(encoded)
        except (OSError, json.JSONDecodeError) as exc:
            raise TierError(f"remote manifest unavailable or corrupt for {key.name}") from exc
        if not isinstance(manifest, dict) or manifest.get("format") != 1 or manifest.get("region") != {
            "dimension": key.dimension, "x": key.x, "z": key.z
        }:
            raise TierError(f"remote manifest identity mismatch for {key.name}")
        return manifest, hashlib.sha256(encoded).hexdigest()

    def manifest_hash(self, key: RegionKey) -> str:
        try:
            return self._hash(self._manifest_path(key))
        except OSError as exc:
            raise TierError(f"remote manifest unavailable for {key.name}") from exc

    def verify(self, key: RegionKey) -> dict:
        manifest = self.read_manifest(key)
        files = manifest.get("files")
        if not isinstance(files, dict) or f"region/r.{key.x}.{key.z}.mca" not in files:
            raise TierError(f"remote region bundle is incomplete for {key.name}")
        for member, info in sorted(files.items()):
            if not self._valid_member(key, member) or not isinstance(info, dict):
                raise TierError(f"invalid bundle member in manifest: {member}")
            source = self._object_path(key) / member
            if not source.is_file() or source.is_symlink():
                raise TierError(f"remote bundle member is missing: {member}")
            if source.stat().st_size != int(info.get("size", -1)) or self._hash(source) != info.get("sha256"):
                raise TierError(f"remote bundle checksum mismatch: {member}")
        return manifest

    def fetch(
        self,
        key: RegionKey,
        destination: Path,
        *,
        expected_manifest: dict | None = None,
        expected_manifest_hash: str | None = None,
    ) -> dict:
        if (expected_manifest is None) != (expected_manifest_hash is None):
            raise TierError("expected manifest and hash must be supplied together")
        if expected_manifest is None:
            manifest, manifest_hash = self.read_manifest_with_hash(key)
        else:
            manifest, manifest_hash = expected_manifest, expected_manifest_hash
        files = manifest.get("files")
        if not isinstance(files, dict) or f"region/r.{key.x}.{key.z}.mca" not in files:
            raise TierError(f"remote region bundle is incomplete for {key.name}")
        destination.mkdir(parents=True, exist_ok=True)
        for member, info in sorted(files.items()):
            if not self._valid_member(key, member) or info.get("name") != PurePosixPath(member).name:
                raise TierError(f"invalid bundle member in manifest: {member}")
            source = self._object_path(key) / member
            if not source.is_file() or source.is_symlink():
                raise TierError(f"remote bundle member is missing: {member}")
            if source.stat().st_size != int(info["size"]):
                raise TierError(f"remote bundle size mismatch: {member}")
            target = destination / member
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.open("rb") as src, target.open("xb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
                dst.flush()
                os.fsync(dst.fileno())
            if self._hash(target) != info["sha256"]:
                raise TierError(f"download verification failed: {member}")
        current_manifest, current_hash = self.read_manifest_with_hash(key)
        if current_hash != manifest_hash or current_manifest != manifest:
            raise TierError(f"remote manifest changed during restore for {key.name}")
        return manifest

    def delete(self, key: RegionKey, *, expected_manifest_hash: str | None = None) -> None:
        with self._lock:
            path = self._object_path(key)
            if path.exists():
                if expected_manifest_hash is not None and self.manifest_hash(key) != expected_manifest_hash:
                    raise TierError(f"remote manifest changed before cleanup for {key.name}")
                shutil.rmtree(path)
                self._fsync_dir(path.parent)

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class TierManager:
    """Crash-journaled mover for tests; caller must provide an exclusive lease."""

    def __init__(
        self,
        server_root: Path,
        journal: Path,
        store: BundleObjectStore,
        acquire_region_lease: Callable[[RegionKey], object],
        local_free_reserve_bytes: int = 15 * 1024**3,
    ):
        if local_free_reserve_bytes < 0:
            raise ValueError("local_free_reserve_bytes must not be negative")
        self.server_root = server_root.resolve()
        self.journal = journal
        self.store = store
        self.acquire_region_lease = acquire_region_lease
        self.local_free_reserve_bytes = local_free_reserve_bytes
        self._operation_lock = threading.RLock()
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS regions (
                dimension TEXT NOT NULL,
                x INTEGER NOT NULL,
                z INTEGER NOT NULL,
                state TEXT NOT NULL,
                last_access REAL NOT NULL,
                manifest_sha256 TEXT,
                error TEXT,
                PRIMARY KEY (dimension, x, z)
            )""")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.journal, timeout=10, isolation_level="IMMEDIATE")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        return db

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        db = self._connect()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def state(self, key: RegionKey) -> str:
        with self._db() as db:
            row = db.execute(
                "SELECT state FROM regions WHERE dimension=? AND x=? AND z=?",
                (key.dimension, key.x, key.z),
            ).fetchone()
        return row[0] if row else "LOCAL_ONLY"

    def last_access(self, key: RegionKey) -> float | None:
        marker_timestamp = self._read_access_marker(key)
        with self._db() as db:
            row = db.execute(
                "SELECT last_access FROM regions WHERE dimension=? AND x=? AND z=?",
                (key.dimension, key.x, key.z),
            ).fetchone()
        database_timestamp = float(row[0]) if row else None
        if marker_timestamp is None:
            return database_timestamp
        return max(marker_timestamp, database_timestamp or 0.0)

    def _access_marker_path(self, key: RegionKey) -> Path:
        dimension_root = self.server_root / DIMENSION_ROOTS[key.dimension]
        if dimension_root.is_symlink() or not dimension_root.resolve().is_relative_to(self.server_root):
            raise TierError(f"unsafe dimension root for access marker: {key.dimension}")
        return dimension_root / ".world-tier" / "access" / f"r.{key.x}.{key.z}.touch"

    def _read_access_marker(self, key: RegionKey) -> float | None:
        marker = self._access_marker_path(key)
        if not marker.exists() and not marker.is_symlink():
            return None
        if marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 32:
            raise TierError(f"invalid region access marker: {marker}")
        try:
            raw_timestamp = marker.read_text(encoding="ascii").strip()
            timestamp_ms = int(raw_timestamp)
        except (OSError, UnicodeError, ValueError) as exc:
            raise TierError(f"invalid region access marker: {marker}") from exc
        if timestamp_ms <= 0:
            raise TierError(f"invalid region access timestamp: {marker}")
        timestamp = timestamp_ms / 1000.0
        now = time.time()
        if timestamp > now + 300:
            # A clock jump must make a region newer, never colder.
            return now
        return timestamp

    def _write_access_marker(self, key: RegionKey, timestamp: float, only_if_missing: bool = False) -> None:
        marker = self._access_marker_path(key)
        access_dir = marker.parent
        for directory in (access_dir.parent, access_dir):
            if directory.is_symlink():
                raise TierError(f"region access directory is a symlink: {directory}")
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not directory.is_dir() or not directory.resolve().is_relative_to(marker.parents[2].resolve()):
                raise TierError(f"unsafe region access directory: {directory}")
            os.chmod(directory, 0o700)
        if marker.is_symlink() or (marker.exists() and not marker.is_file()):
            raise TierError(f"invalid region access marker: {marker}")
        encoded = f"{int(timestamp * 1000)}\n".encode("ascii")
        temporary = marker.with_name(marker.name + ".tmp-" + uuid.uuid4().hex)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(temporary, flags, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
            if only_if_missing:
                try:
                    os.link(temporary, marker, follow_symlinks=False)
                except FileExistsError:
                    pass
                else:
                    DirectoryObjectStore._fsync_dir(access_dir)
            else:
                os.replace(temporary, marker)
                DirectoryObjectStore._fsync_dir(access_dir)
        except OSError as exc:
            raise TierError(f"could not persist region access marker: {marker}") from exc
        finally:
            if temporary.exists():
                temporary.unlink()

    def record_access(self, key: RegionKey, when: float | None = None) -> None:
        timestamp = time.time() if when is None else when
        if not math.isfinite(timestamp) or timestamp <= 0:
            raise ValueError("access timestamp must be a positive finite value")
        previous = self._read_access_marker(key)
        if previous is not None:
            timestamp = max(timestamp, previous)
        self._write_access_marker(key, timestamp)
        with self._db() as db:
            db.execute("""INSERT INTO regions(dimension,x,z,state,last_access)
                VALUES(?,?,?,'LOCAL_ONLY',?)
                ON CONFLICT(dimension,x,z) DO UPDATE SET last_access=excluded.last_access""",
                (key.dimension, key.x, key.z, timestamp))

    def catalog_local_regions(
        self, when: float | None = None, trust_atime: bool = False
    ) -> int:
        """Seed unseen local regions with a conservative tracking-start time.

        Existing files are never aged from mtime. By default they age from the
        moment tracking starts; a caller may opt into filesystem atime only
        after verifying that the mount updates it reliably.
        """
        baseline = time.time() if when is None else when
        discovered: dict[RegionKey, float] = {}
        for key in self.local_regions():
            last_access = baseline
            if when is None and trust_atime:
                try:
                    members = RegionBundle(self.server_root, key).files()
                    last_access = max(member.stat().st_atime for member in members.values())
                except FileNotFoundError:
                    # Concurrent removal or rename: age this entry from now.
                    last_access = baseline
            marker_access = self._read_access_marker(key)
            if marker_access is not None:
                last_access = max(marker_access, last_access) if trust_atime else marker_access
            else:
                # Existing terrain with no trustworthy history starts hot.
                self._write_access_marker(key, last_access, only_if_missing=True)
                marker_access = self._read_access_marker(key)
                if marker_access is not None:
                    last_access = max(last_access, marker_access)
            discovered[key] = last_access
        with self._db() as db:
            db.executemany("""INSERT INTO regions
                (dimension,x,z,state,last_access) VALUES(?,?,?,'LOCAL_ONLY',?)
                ON CONFLICT(dimension,x,z) DO UPDATE SET
                  last_access=MAX(regions.last_access,excluded.last_access)""",
                ((key.dimension, key.x, key.z, discovered[key]) for key in sorted(discovered)))
        return len(discovered)

    def local_regions(self) -> list[RegionKey]:
        """Return regions with a local terrain file, rejecting unsafe paths."""
        found: list[RegionKey] = []
        for dimension, relative_root in DIMENSION_ROOTS.items():
            dimension_root = self.server_root / relative_root
            directory = dimension_root / "region"
            if dimension_root.is_symlink() or directory.is_symlink():
                raise TierError(f"symlink in local region path: {directory}")
            if not directory.exists():
                continue
            if not directory.is_dir():
                raise TierError(f"local region path is not a directory: {directory}")
            for path in directory.iterdir():
                if path.is_symlink():
                    raise TierError(f"symlink in local region directory: {path}")
                match = re.fullmatch(r"r\.(-?\d+)\.(-?\d+)\.mca", path.name)
                if not match:
                    continue
                if not path.is_file():
                    raise TierError(f"non-file region entry: {path}")
                found.append(RegionKey(dimension, int(match.group(1)), int(match.group(2))))
        return sorted(found)

    def local_region_size(self, key: RegionKey) -> int:
        """Return the complete local bundle size, including external .mcc files."""
        return sum(path.stat().st_size for path in RegionBundle(self.server_root, key).files().values())

    def local_region_remote_size(self, key: RegionKey) -> int:
        """Estimate exact published bytes, including the deterministic manifest."""
        files = RegionBundle(self.server_root, key).files()
        manifest_files = {
            member: {
                "name": path.name,
                "size": path.stat().st_size,
                # Every published SHA-256 digest has this fixed encoded length.
                "sha256": "0" * 64,
            }
            for member, path in sorted(files.items())
        }
        manifest = {
            "format": 1,
            "region": {"dimension": key.dimension, "x": key.x, "z": key.z},
            "files": manifest_files,
        }
        encoded_size = len(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")) + 1
        return sum(info["size"] for info in manifest_files.values()) + encoded_size

    def plan_local_budget(
        self,
        target_bytes: int,
        age_seconds: float,
        protected_regions: set[str] | None = None,
        now: float | None = None,
        remote_available_bytes: int | None = None,
        max_selected: int | None = None,
    ) -> dict:
        """Choose oldest cold local bundles needed to meet a dry-run size target."""
        if type(target_bytes) is not int or target_bytes < 0:
            raise ValueError("local world-data target must be a non-negative integer")
        if max_selected is not None and (type(max_selected) is not int or max_selected < 1):
            raise ValueError("maximum selected bundles must be a positive integer")
        if (remote_available_bytes is not None
                and (type(remote_available_bytes) is not int or remote_available_bytes < 0)):
            raise ValueError("remote available quota must be a non-negative integer")
        local_sizes = {key: self.local_region_size(key) for key in self.local_regions()}
        current_bytes = sum(local_sizes.values())
        needed_bytes = max(0, current_bytes - target_bytes)
        protected = protected_regions or set()
        selected: list[dict] = []
        planned_bytes = 0
        planned_remote_bytes = 0
        if needed_bytes:
            for key in self.cold_regions(age_seconds, now):
                size = local_sizes.get(key)
                if size is None or key.name in protected:
                    continue
                last_access = self.last_access(key)
                if last_access is None:
                    continue
                remote_size = self.local_region_remote_size(key)
                if (remote_available_bytes is not None
                        and planned_remote_bytes + remote_size > remote_available_bytes):
                    continue
                selected.append({
                    "key": key,
                    "bytes": size,
                    "remote_bytes": remote_size,
                    "last_access": last_access,
                })
                planned_bytes += size
                planned_remote_bytes += remote_size
                if planned_bytes >= needed_bytes or (
                    max_selected is not None and len(selected) >= max_selected
                ):
                    break
        return {
            "current_bytes": current_bytes,
            "target_bytes": target_bytes,
            "needed_bytes": needed_bytes,
            "planned_bytes": planned_bytes,
            "planned_remote_bytes": planned_remote_bytes,
            "remote_available_bytes": remote_available_bytes,
            "unmet_bytes": max(0, needed_bytes - planned_bytes),
            "selected": selected,
        }

    def cold_regions(self, age_seconds: float, now: float | None = None) -> list[RegionKey]:
        if not math.isfinite(age_seconds) or age_seconds < 0:
            raise ValueError("cold age must be a finite non-negative number")
        cutoff = (time.time() if now is None else now) - age_seconds
        with self._db() as db:
            rows = db.execute("""SELECT dimension,x,z FROM regions
                WHERE state IN ('LOCAL_ONLY','CACHED') AND last_access<=?
                ORDER BY last_access ASC""", (cutoff,)).fetchall()
        return [RegionKey(*row) for row in rows]

    def _set_state(self, key: RegionKey, state: str, manifest_hash: str | None = None,
                   error: str | None = None) -> None:
        with self._db() as db:
            db.execute("""INSERT INTO regions(dimension,x,z,state,last_access,manifest_sha256,error)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(dimension,x,z) DO UPDATE SET
                  state=excluded.state,
                  manifest_sha256=COALESCE(excluded.manifest_sha256,regions.manifest_sha256),
                  error=excluded.error""",
                (key.dimension, key.x, key.z, state, time.time(), manifest_hash, error))

    def _marker_path(self, key: RegionKey) -> Path:
        dimension_root = self.server_root / DIMENSION_ROOTS[key.dimension]
        if dimension_root.is_symlink() or not dimension_root.resolve().is_relative_to(self.server_root):
            raise TierError(f"unsafe dimension root for marker: {key.dimension}")
        marker = dimension_root / ".world-tier" / "remote" / f"r.{key.x}.{key.z}.json"
        for directory in (marker.parent.parent, marker.parent):
            if directory.is_symlink():
                raise TierError(f"world-tier marker directory is a symlink: {directory}")
            if directory.exists() and not directory.resolve().is_relative_to(dimension_root.resolve()):
                raise TierError(f"world-tier marker directory escapes the dimension root: {directory}")
        return marker

    def _write_remote_marker(self, key: RegionKey, manifest_hash: str) -> None:
        marker = self._marker_path(key)
        marker_dir = marker.parent
        for directory in (marker.parent.parent, marker_dir):
            if directory.is_symlink():
                raise TierError(f"world-tier marker directory is a symlink: {directory}")
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not directory.is_dir() or directory.resolve().parent != directory.parent.resolve():
                raise TierError(f"unsafe world-tier marker directory: {directory}")
        payload = {
            "format": 1,
            "state": "REMOTE_ONLY",
            "dimension": key.dimension,
            "dimension_key": DIMENSION_KEYS[key.dimension],
            "x": key.x,
            "z": key.z,
            "manifest_sha256": manifest_hash,
        }
        encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        if marker.exists() or marker.is_symlink():
            if marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 4096 or marker.read_bytes() != encoded:
                raise TierError(f"an unrelated or invalid world-tier marker already exists: {marker}")
            return
        temporary = marker.with_name(marker.name + ".tmp-" + uuid.uuid4().hex)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(temporary, flags, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, marker)
            DirectoryObjectStore._fsync_dir(marker_dir)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _remove_remote_marker(self, key: RegionKey, manifest_hash: str | None) -> None:
        marker = self._marker_path(key)
        if not marker.exists() and not marker.is_symlink():
            return
        if marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 4096:
            raise TierError(f"invalid world-tier marker: {marker}")
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TierError(f"invalid world-tier marker: {marker}") from exc
        if (payload.get("format") != 1 or payload.get("dimension") != key.dimension
                or payload.get("dimension_key") != DIMENSION_KEYS[key.dimension]
                or type(payload.get("x")) is not int or payload.get("x") != key.x
                or type(payload.get("z")) is not int or payload.get("z") != key.z
                or payload.get("state") not in ("REMOTE_ONLY", "EVICTING", "RESTORING")
                or (manifest_hash is not None and payload.get("manifest_sha256") != manifest_hash)):
            raise TierError(f"world-tier marker identity or checksum mismatch: {marker}")
        marker.unlink()
        DirectoryObjectStore._fsync_dir(marker.parent)

    def _set_remote_marker_restoring(self, key: RegionKey, manifest_hash: str | None) -> None:
        marker = self._marker_path(key)
        if not marker.exists() and not marker.is_symlink():
            return
        if marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 4096:
            raise TierError(f"invalid world-tier marker: {marker}")
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TierError(f"invalid world-tier marker: {marker}") from exc
        if (payload.get("format") != 1 or payload.get("dimension") != key.dimension
                or payload.get("dimension_key") != DIMENSION_KEYS[key.dimension]
                or type(payload.get("x")) is not int or payload.get("x") != key.x
                or type(payload.get("z")) is not int or payload.get("z") != key.z
                or (manifest_hash is not None and payload.get("manifest_sha256") != manifest_hash)
                or payload.get("state") not in ("REMOTE_ONLY", "EVICTING", "RESTORING")):
            raise TierError(f"world-tier marker identity, state, or checksum mismatch: {marker}")
        payload["state"] = "RESTORING"
        encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        temporary = marker.with_name(marker.name + ".tmp-" + uuid.uuid4().hex)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(temporary, flags, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, marker)
            DirectoryObjectStore._fsync_dir(marker.parent)
        finally:
            if temporary.exists():
                temporary.unlink()

    def offload(self, key: RegionKey) -> None:
        with self._operation_lock:
            with self.acquire_region_lease(key) as acquired:
                if not acquired:
                    raise TierError(f"region is busy; refusing offload: {key.name}")
                bundle = RegionBundle(self.server_root, key)
                files = bundle.files()
                self._set_state(key, "UPLOADING")
                try:
                    _, manifest_hash = self.store.publish(key, files)
                except Exception as exc:
                    self._set_state(key, "LOCAL_ONLY", error=type(exc).__name__)
                    raise

                self._set_state(key, "EVICTING", manifest_hash)
                self._write_remote_marker(key, manifest_hash)
                # Production must retain the Paper region lease across upload,
                # verification, and eviction so a reader cannot race this move.
                for path in files.values():
                    path.unlink()
                    self.store._fsync_dir(path.parent)
                self._set_state(key, "REMOTE_ONLY", manifest_hash)

    def restore(self, key: RegionKey, expected_manifest_hash: str | None = None) -> None:
        with self._operation_lock:
            with self.acquire_region_lease(key) as acquired:
                if not acquired:
                    raise TierError(f"region is busy; refusing restore: {key.name}")
                if expected_manifest_hash is None and self.state(key) not in ("REMOTE_ONLY", "RESTORING", "EVICTING"):
                    return
                journal_hash = self._stored_manifest_hash(key)
                if expected_manifest_hash is not None and journal_hash is not None and journal_hash != expected_manifest_hash:
                    raise TierError(f"marker manifest checksum does not match the journal for {key.name}")
                expected_manifest_hash = expected_manifest_hash or journal_hash
                try:
                    manifest_before, manifest_hash_before = self.store.read_manifest_with_hash(key)
                except TierError as exc:
                    if expected_manifest_hash is not None:
                        try:
                            current_manifest_hash = self.store.manifest_hash(key)
                        except TierError:
                            raise exc
                        if current_manifest_hash != expected_manifest_hash:
                            raise TierError(f"remote manifest checksum mismatch for {key.name}") from exc
                    raise
                if expected_manifest_hash and manifest_hash_before != expected_manifest_hash:
                    raise TierError(f"remote manifest checksum mismatch for {key.name}")
                expected_manifest_hash = expected_manifest_hash or manifest_hash_before
                files = manifest_before.get("files")
                if not isinstance(files, dict) or not files:
                    raise TierError(f"remote region bundle is incomplete for {key.name}")
                required_bytes = 0
                for member, info in files.items():
                    if (not self.store._valid_member(key, member) or not isinstance(info, dict)
                            or type(info.get("size")) is not int or info["size"] < 0):
                        raise TierError(f"remote manifest has invalid bundle entry for {key.name}")
                    required_bytes += info["size"]
                if required_bytes <= 0:
                    raise TierError(f"remote manifest has invalid sizes for {key.name}")
                if shutil.disk_usage(self.server_root).free - required_bytes < self.local_free_reserve_bytes:
                    raise TierError(f"insufficient local free space to restore {key.name} safely")
                self._set_state(key, "RESTORING")
                self._set_remote_marker_restoring(key, expected_manifest_hash)
                stage_root = self.server_root / ".world-tier-stage"
                stage_root.mkdir(parents=True, exist_ok=True)
                stage = Path(tempfile.mkdtemp(prefix="restore-", dir=stage_root))
                try:
                    manifest = self.store.fetch(
                        key,
                        stage,
                        expected_manifest=manifest_before,
                        expected_manifest_hash=expected_manifest_hash,
                    )
                    if manifest != manifest_before:
                        raise TierError(f"remote manifest changed during restore for {key.name}")
                    if shutil.disk_usage(self.server_root).free < self.local_free_reserve_bytes:
                        raise TierError(f"local free-space reserve would be breached by restoring {key.name}")
                    root = self.server_root / DIMENSION_ROOTS[key.dimension]
                    for member in sorted(manifest["files"]):
                        if not self.store._valid_member(key, member):
                            raise TierError(f"invalid bundle member in manifest: {member}")
                        relative = Path(*PurePosixPath(member).parts)
                        destination_dir = root / relative.parent
                        destination_dir.mkdir(parents=True, exist_ok=True)
                        staged_file = stage / relative
                        destination = root / relative
                        os.replace(staged_file, destination)
                        DirectoryObjectStore._fsync_dir(destination_dir)
                    # Keep the remote object until every local member is published.
                    # Removing the marker first makes a crash leave an extra remote
                    # copy, never a marker pointing at an already-deleted object.
                    self._remove_remote_marker(key, expected_manifest_hash)
                    self._set_state(key, "CACHED")
                    self.store.delete(key, expected_manifest_hash=expected_manifest_hash)
                    self._set_state(key, "LOCAL_ONLY")
                finally:
                    if stage.exists():
                        shutil.rmtree(stage)

    def _stored_manifest_hash(self, key: RegionKey) -> str | None:
        with self._db() as db:
            row = db.execute(
                "SELECT manifest_sha256 FROM regions WHERE dimension=? AND x=? AND z=?",
                (key.dimension, key.x, key.z),
            ).fetchone()
        return row[0] if row else None

    def recover(self, key: RegionKey) -> None:
        state = self.state(key)
        if state == "UPLOADING":
            # Uploading never removes local data; restart from the safe local copy.
            self._set_state(key, "LOCAL_ONLY", error="recovered interrupted upload")
        elif state in ("REMOTE_ONLY", "RESTORING"):
            self.restore(key)
        elif state == "EVICTING":
            # Re-run the verified remote restore to heal a partially evicted bundle.
            self.restore(key)
