from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath
from typing import Callable, Iterator, Mapping


DIMENSION_ROOTS = {
    "overworld": Path("world"),
    "nether": Path("world_nether/DIM-1"),
    "end": Path("world_the_end/DIM1"),
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
        try:
            with self._manifest_path(key).open("r", encoding="utf-8") as source:
                manifest = json.load(source)
        except (OSError, json.JSONDecodeError) as exc:
            raise TierError(f"remote manifest unavailable or corrupt for {key.name}") from exc
        if manifest.get("format") != 1 or manifest.get("region") != {
            "dimension": key.dimension, "x": key.x, "z": key.z
        }:
            raise TierError(f"remote manifest identity mismatch for {key.name}")
        return manifest

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

    def fetch(self, key: RegionKey, destination: Path) -> dict:
        manifest = self.read_manifest(key)
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
        return manifest

    def delete(self, key: RegionKey) -> None:
        with self._lock:
            path = self._object_path(key)
            if path.exists():
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
        store: DirectoryObjectStore,
        acquire_region_lease: Callable[[RegionKey], object],
    ):
        self.server_root = server_root.resolve()
        self.journal = journal
        self.store = store
        self.acquire_region_lease = acquire_region_lease
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

    def record_access(self, key: RegionKey, when: float | None = None) -> None:
        timestamp = time.time() if when is None else when
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
        for dimension, relative_root in DIMENSION_ROOTS.items():
            directory = self.server_root / relative_root / "region"
            if not directory.exists():
                continue
            for path in directory.iterdir():
                if path.is_symlink():
                    raise TierError(f"symlink in local region directory: {path}")
                match = re.fullmatch(r"r\.(-?\d+)\.(-?\d+)\.mca", path.name)
                if not match:
                    continue
                if not path.is_file():
                    raise TierError(f"non-file region entry: {path}")
                key = RegionKey(dimension, int(match.group(1)), int(match.group(2)))
                last_access = baseline
                if when is None and trust_atime:
                    try:
                        members = RegionBundle(self.server_root, key).files()
                        last_access = max(member.stat().st_atime for member in members.values())
                    except FileNotFoundError:
                        # Concurrent removal or rename: age this entry from now.
                        last_access = baseline
                discovered[key] = last_access
        with self._db() as db:
            db.executemany("""INSERT OR IGNORE INTO regions
                (dimension,x,z,state,last_access) VALUES(?,?,?,'LOCAL_ONLY',?)""",
                ((key.dimension, key.x, key.z, discovered[key]) for key in sorted(discovered)))
        return len(discovered)

    def cold_regions(self, age_seconds: float, now: float | None = None) -> list[RegionKey]:
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
                # Production must retain the Paper region lease across upload,
                # verification, and eviction so a reader cannot race this move.
                for path in files.values():
                    path.unlink()
                    self.store._fsync_dir(path.parent)
                self._set_state(key, "REMOTE_ONLY", manifest_hash)

    def restore(self, key: RegionKey) -> None:
        with self._operation_lock:
            with self.acquire_region_lease(key) as acquired:
                if not acquired:
                    raise TierError(f"region is busy; refusing restore: {key.name}")
                if self.state(key) not in ("REMOTE_ONLY", "RESTORING", "EVICTING"):
                    return
                expected_manifest_hash = self._stored_manifest_hash(key)
                if expected_manifest_hash and self.store.manifest_hash(key) != expected_manifest_hash:
                    raise TierError(f"remote manifest checksum mismatch for {key.name}")
                self._set_state(key, "RESTORING")
                stage_root = self.server_root / ".world-tier-stage"
                stage_root.mkdir(parents=True, exist_ok=True)
                stage = Path(tempfile.mkdtemp(prefix="restore-", dir=stage_root))
                try:
                    manifest = self.store.fetch(key, stage)
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
                    # Delete the remote copy only after every local member is published.
                    self._set_state(key, "CACHED")
                    self.store.delete(key)
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
