from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterator, Mapping, Protocol

from .store import DirectoryObjectStore, RegionKey, TierError


@dataclass(frozen=True)
class OpenSSHConfig:
    """Connection details for a dedicated SFTP object root.

    Keep identity_file, known_hosts, and this configuration outside the public
    repository. The host key must already be pinned in known_hosts.
    """

    host: str
    username: str
    port: int
    identity_file: Path
    known_hosts: Path
    remote_root: PurePosixPath
    max_bytes: int
    control_path: Path | None = None
    timeout_seconds: int = 900
    lock_timeout_seconds: int = 30

    def validate(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9.-]+", self.host):
            raise ValueError("host must be a DNS name or IP address")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9._-]*", self.username):
            raise ValueError("invalid SSH username")
        if not 1 <= self.port <= 65535:
            raise ValueError("SSH port must be between 1 and 65535")
        if self.max_bytes <= 0 or self.timeout_seconds <= 0 or self.lock_timeout_seconds <= 0:
            raise ValueError("size and timeout limits must be positive")
        root = self.remote_root
        if (not root.is_absolute() or root.parts[0] != "/" or len(root.parts) < 4
                or root.parts[1] != "home" or root.parts[2] != self.username):
            raise ValueError("remote_root must be a dedicated directory beneath the SSH user's home")
        if any(part in (".", "..") or not re.fullmatch(r"[A-Za-z0-9._-]+", part) for part in root.parts[1:]):
            raise ValueError("remote_root may only contain simple path components")
        for label, path in (("identity file", self.identity_file), ("known_hosts", self.known_hosts)):
            if not path.is_absolute():
                raise ValueError(f"{label} path must be absolute")
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"{label} must be a regular file")
        mode = stat.S_IMODE(self.identity_file.stat().st_mode)
        if mode & 0o077:
            raise ValueError("identity file permissions must exclude group and other access")
        if self.control_path is not None:
            if not self.control_path.is_absolute() or self.control_path.name.count("%C") != 1:
                raise ValueError("control_path must be absolute and contain exactly one %C token")
            if "\n" in str(self.control_path) or "\r" in str(self.control_path) or "\x00" in str(self.control_path):
                raise ValueError("control_path contains an invalid character")
            if self.control_path.parent.is_symlink() or not self.control_path.parent.is_dir():
                raise ValueError("control_path parent must be an existing private directory")
            control_dir = self.control_path.parent.stat()
            if control_dir.st_uid != os.geteuid():
                raise ValueError("control_path parent must be owned by the current user")
            control_dir_mode = stat.S_IMODE(control_dir.st_mode)
            if control_dir_mode & 0o077:
                raise ValueError("control_path parent permissions must exclude group and other access")


class SSHTransport(Protocol):
    def run_ssh(self, command: str, *, check: bool = True) -> subprocess.CompletedProcess[str]: ...

    def run_sftp(self, batch: str) -> subprocess.CompletedProcess[str]: ...


class OpenSSHTransport:
    """Small subprocess wrapper with strict host-key and batch-mode checks."""

    def __init__(self, config: OpenSSHConfig):
        self.config = config
        self.target = f"{config.username}@{config.host}"
        self.options = [
            "-p", str(config.port),
            "-i", str(config.identity_file),
            "-o", "BatchMode=yes",
            "-o", "IdentitiesOnly=yes",
            "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={config.known_hosts}",
            "-o", f"ConnectTimeout={min(config.timeout_seconds, 30)}",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3",
        ]
        if config.control_path is not None:
            self.options.extend([
                "-o", "ControlMaster=auto",
                "-o", "ControlPersist=30",
                "-o", f"ControlPath={config.control_path}",
            ])

    def run_ssh(self, command: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["ssh", "-T", *self.options, self.target, command],
            text=True,
            capture_output=True,
            timeout=self.config.timeout_seconds,
            check=False,
        )
        if check and result.returncode != 0:
            raise TierError(f"remote SSH operation failed (exit {result.returncode})")
        return result

    def run_sftp(self, batch: str) -> subprocess.CompletedProcess[str]:
        sftp_options = ["-P", str(self.config.port), *self.options[2:]]
        result = subprocess.run(
            ["sftp", "-b", "-", *sftp_options, self.target],
            input=batch,
            text=True,
            capture_output=True,
            timeout=self.config.timeout_seconds,
            check=False,
        )
        if result.returncode != 0:
            raise TierError(f"SFTP operation failed (exit {result.returncode})")
        return result


class OpenSSHObjectStore:
    """Quota-bounded remote object store using only pinned OpenSSH/SFTP.

    Objects are staged under the configured remote root, verified locally, and
    published with a same-filesystem directory rename. A remote mkdir lock
    prevents simultaneous writers. Stale locks are deliberately not broken
    automatically: an operator must verify no transfer is still active.
    """

    def __init__(self, config: OpenSSHConfig, transport: SSHTransport | None = None):
        config.validate()
        self.config = config
        self.max_bytes = config.max_bytes
        self.root = config.remote_root
        self.objects = self.root / "objects"
        self.staging = self.root / ".staging"
        self.lock_path = self.root / ".writer-lock"
        self.transport = transport or OpenSSHTransport(config)
        self._lock = threading.RLock()

    @staticmethod
    def _hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for part in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(part)
        return digest.hexdigest()

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _sftp_quote(value: str | Path) -> str:
        text = str(value)
        if "\n" in text or "\r" in text or "\x00" in text:
            raise TierError("newline or NUL in SFTP path")
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def _remote_path(self, path: PurePosixPath) -> str:
        if not path.is_relative_to(self.root):
            raise TierError("remote path escapes configured root")
        return str(path)

    def _ssh(self, command: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            result = self.transport.run_ssh(command, check=check)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TierError("remote SSH connection failed") from exc
        if check and result.returncode != 0:
            raise TierError(f"remote SSH operation failed (exit {result.returncode})")
        return result

    def _sftp(self, lines: list[str]) -> None:
        try:
            result = self.transport.run_sftp("\n".join(lines) + "\n")
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TierError("SFTP connection failed") from exc
        if result.returncode != 0:
            raise TierError(f"SFTP operation failed (exit {result.returncode})")

    def _ensure_root(self) -> None:
        objects = shlex.quote(str(self.objects))
        staging = shlex.quote(str(self.staging))
        self._ssh(f"umask 077 && mkdir -p -- {objects} {staging} && chmod 700 -- {objects} {staging}")

    @contextmanager
    def _remote_writer_lock(self) -> Iterator[None]:
        lock = shlex.quote(str(self.lock_path))
        deadline = time.monotonic() + self.config.lock_timeout_seconds
        while True:
            result = self._ssh(f"umask 077 && mkdir -- {lock}", check=False)
            if result.returncode == 0:
                break
            if result.returncode != 1:
                raise TierError(f"could not acquire remote writer lock (exit {result.returncode})")
            exists = self._ssh(f"test -d {lock}", check=False)
            if exists.returncode != 0:
                raise TierError("remote writer lock creation failed")
            if time.monotonic() >= deadline:
                raise TierError("remote writer lock is busy; inspect it before retrying")
            time.sleep(0.5)
        try:
            yield
        finally:
            released = self._ssh(f"rmdir -- {lock}", check=False)
            if released.returncode != 0:
                raise TierError("could not release remote writer lock; inspect it before retrying")

    def _object_path(self, key: RegionKey) -> PurePosixPath:
        return self.objects / key.dimension / f"r.{key.x}.{key.z}"

    _valid_member = staticmethod(DirectoryObjectStore._valid_member)

    def _manifest_path(self, key: RegionKey) -> PurePosixPath:
        return self._object_path(key) / "manifest.json"

    def _exists(self, path: PurePosixPath) -> bool:
        result = self._ssh(f"test -e {shlex.quote(self._remote_path(path))}", check=False)
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        raise TierError("could not query remote object state")

    def usage(self) -> int:
        self._ensure_root()
        with self._lock, self._remote_writer_lock():
            return self._usage_unlocked()

    def _usage_unlocked(self) -> int:
        result = self._ssh(
            "du -sb -- " + shlex.quote(str(self.objects)) + " " + shlex.quote(str(self.staging))
        )
        try:
            return sum(int(line.split()[0]) for line in result.stdout.splitlines() if line.split())
        except ValueError as exc:
            raise TierError("remote storage usage response is invalid") from exc

    def _manifest(self, key: RegionKey) -> tuple[dict, bytes]:
        with tempfile.TemporaryDirectory(prefix="fiesta-tier-manifest-") as temporary:
            local = Path(temporary) / "manifest.json"
            self._sftp([f"get {self._sftp_quote(self._manifest_path(key))} {self._sftp_quote(local)}"])
            try:
                encoded = local.read_bytes()
                manifest = json.loads(encoded)
            except (OSError, json.JSONDecodeError) as exc:
                raise TierError(f"remote manifest unavailable or corrupt for {key.name}") from exc
        if not isinstance(manifest, dict) or manifest.get("format") != 1 or manifest.get("region") != {
            "dimension": key.dimension, "x": key.x, "z": key.z
        }:
            raise TierError(f"remote manifest identity mismatch for {key.name}")
        files = manifest.get("files")
        if not isinstance(files, dict) or f"region/r.{key.x}.{key.z}.mca" not in files:
            raise TierError(f"remote region bundle is incomplete for {key.name}")
        for member, info in files.items():
            if not DirectoryObjectStore._valid_member(key, member) or not isinstance(info, dict):
                raise TierError(f"invalid remote bundle member: {member}")
            if info.get("name") != PurePosixPath(member).name:
                raise TierError(f"invalid remote bundle filename: {member}")
            if type(info.get("size")) is not int or info["size"] < 0:
                raise TierError(f"invalid remote bundle size: {member}")
            if not re.fullmatch(r"[0-9a-f]{64}", str(info.get("sha256", ""))):
                raise TierError(f"invalid remote bundle checksum: {member}")
        return manifest, encoded

    def read_manifest(self, key: RegionKey) -> dict:
        return self._manifest(key)[0]

    def manifest_hash(self, key: RegionKey) -> str:
        return hashlib.sha256(self._manifest(key)[1]).hexdigest()

    def verify(self, key: RegionKey) -> dict:
        with self._lock, self._remote_writer_lock():
            return self._verify_unlocked(key)

    def _verify_unlocked(self, key: RegionKey) -> dict:
        manifest, _encoded = self._manifest(key)
        with tempfile.TemporaryDirectory(prefix="fiesta-tier-verify-") as temporary:
            self._fetch_unlocked(key, Path(temporary))
        return manifest

    def publish(self, key: RegionKey, files: Mapping[str, Path]) -> tuple[dict, str]:
        with self._lock:
            self._ensure_root()
            with self._remote_writer_lock():
                manifest_files: dict[str, dict[str, int | str]] = {}
                before: dict[str, tuple[int, int, int, int]] = {}
                for member, source in sorted(files.items()):
                    if not DirectoryObjectStore._valid_member(key, member) or source.is_symlink() or not source.is_file():
                        raise TierError(f"invalid bundle member: {source}")
                    st = source.stat()
                    before[member] = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
                    manifest_files[member] = {
                        "name": source.name,
                        "size": st.st_size,
                        "sha256": self._hash(source),
                    }
                    after = source.stat()
                    if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != before[member]:
                        raise TierError(f"region file changed while being read: {source}")

                terrain_member = f"region/r.{key.x}.{key.z}.mca"
                if terrain_member not in manifest_files:
                    raise TierError("bundle must contain terrain region data")
                generation = {
                    "format": 1,
                    "region": {"dimension": key.dimension, "x": key.x, "z": key.z},
                    "files": manifest_files,
                }
                encoded = (json.dumps(generation, sort_keys=True, separators=(",", ":")) + "\n").encode()
                manifest_hash = hashlib.sha256(encoded).hexdigest()
                total = sum(int(info["size"]) for info in manifest_files.values()) + len(encoded)

                final = self._object_path(key)
                if self._exists(final):
                    if self._verify_unlocked(key) == generation:
                        return generation, manifest_hash
                    raise TierError(f"remote object already exists with different contents: {key.name}")
                if self._usage_unlocked() + total > self.max_bytes:
                    raise TierError("remote storage quota would be exceeded")

                stage = self.staging / f"{key.dimension}-r.{key.x}.{key.z}-{uuid.uuid4().hex}"
                stage_s = self._remote_path(stage)
                final_s = self._remote_path(final)
                parent = shlex.quote(str(final.parent))
                self._ssh(f"mkdir -p -- {parent}")
                batch = [f"mkdir {self._sftp_quote(stage)}"]
                created_dirs: set[PurePosixPath] = set()
                with tempfile.TemporaryDirectory(prefix="fiesta-tier-publish-") as temporary:
                    manifest_file = Path(temporary) / "manifest.json"
                    manifest_file.write_bytes(encoded)
                    for member, source in sorted(files.items()):
                        directory = stage / PurePosixPath(member).parent
                        if directory not in created_dirs:
                            batch.append(f"mkdir {self._sftp_quote(directory)}")
                            created_dirs.add(directory)
                        batch.append(f"put -p {self._sftp_quote(source)} {self._sftp_quote(stage / member)}")
                    batch.append(f"put -p {self._sftp_quote(manifest_file)} {self._sftp_quote(stage / 'manifest.json')}")
                    try:
                        self._sftp(batch)
                        verified_root = Path(temporary) / "verify"
                        verification_batch: list[str] = []
                        for member in sorted(files):
                            target = verified_root.joinpath(*PurePosixPath(member).parts)
                            target.parent.mkdir(parents=True, exist_ok=True)
                            verification_batch.append(
                                f"get {self._sftp_quote(stage / member)} {self._sftp_quote(target)}"
                            )
                        verified_manifest = verified_root / "manifest.json"
                        verification_batch.append(
                            f"get {self._sftp_quote(stage / 'manifest.json')} {self._sftp_quote(verified_manifest)}"
                        )
                        self._sftp(verification_batch)
                        for member, info in manifest_files.items():
                            target = verified_root.joinpath(*PurePosixPath(member).parts)
                            if target.stat().st_size != int(info["size"]) or self._hash(target) != info["sha256"]:
                                raise TierError(f"remote upload verification failed for {member}")
                        if verified_manifest.read_bytes() != encoded:
                            raise TierError("remote manifest upload verification failed")
                        self._sftp([
                            f"rename {self._sftp_quote(stage)} {self._sftp_quote(final)}"
                        ])
                    except Exception:
                        self._ssh(f"rm -rf -- {shlex.quote(stage_s)}", check=False)
                        raise
                return generation, manifest_hash

    def fetch(self, key: RegionKey, destination: Path) -> dict:
        with self._lock, self._remote_writer_lock():
            return self._fetch_unlocked(key, destination)

    def _fetch_unlocked(self, key: RegionKey, destination: Path) -> dict:
        manifest, _encoded = self._manifest(key)
        destination.mkdir(parents=True, exist_ok=True)
        object_path = self._object_path(key)
        batch: list[str] = []
        targets: list[tuple[str, dict, Path]] = []
        for member, info in sorted(manifest["files"].items()):
            relative = PurePosixPath(member)
            target = destination.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            batch.append(f"get {self._sftp_quote(object_path / relative)} {self._sftp_quote(target)}")
            targets.append((member, info, target))
        self._sftp(batch)
        for member, info, target in targets:
            if target.is_symlink() or not target.is_file():
                raise TierError(f"downloaded bundle member is not a regular file: {member}")
            if target.stat().st_size != int(info["size"]) or self._hash(target) != info["sha256"]:
                raise TierError(f"download verification failed: {member}")
            with target.open("rb") as file:
                os.fsync(file.fileno())
        return manifest

    def delete(self, key: RegionKey) -> None:
        with self._lock, self._remote_writer_lock():
            manifest, _encoded = self._manifest(key)
            object_path = self._object_path(key)
            lines = [f"rm {self._sftp_quote(object_path / PurePosixPath(member))}" for member in sorted(manifest["files"])]
            lines.append(f"rm {self._sftp_quote(object_path / 'manifest.json')}")
            directories = sorted(
                {object_path / PurePosixPath(member).parent for member in manifest["files"] if PurePosixPath(member).parent != PurePosixPath(".")},
                key=lambda path: len(path.parts),
                reverse=True,
            )
            lines.extend(f"rmdir {self._sftp_quote(path)}" for path in directories)
            lines.append(f"rmdir {self._sftp_quote(object_path)}")
            self._sftp(lines)


__all__ = ["OpenSSHConfig", "OpenSSHObjectStore", "OpenSSHTransport", "SSHTransport"]
