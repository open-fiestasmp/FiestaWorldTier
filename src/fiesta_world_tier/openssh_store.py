from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
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
from typing import Iterator, Mapping, Protocol, Sequence

from .store import DirectoryObjectStore, RegionKey, TierError

MAX_MANIFEST_BYTES = 1024 * 1024
MAX_BUNDLE_FILES = 4096
TRANSIENT_SSH_DIAGNOSTIC = re.compile(
    r"(?:connection refused|connection timed out|operation timed out|no route to host|"
    r"network is unreachable|connection reset by peer|connection aborted|"
    r"temporary failure in name resolution|"
    r"connection closed by (?:remote host|[^\r\n]+ port \d+))",
    re.IGNORECASE,
)


def _ssh_transport_failure(operation: str, diagnostic: str) -> TierError:
    if TRANSIENT_SSH_DIAGNOSTIC.search(diagnostic):
        return TransientSSHError(f"{operation} transport disconnected")
    return TierError(f"{operation} failed (exit 255)")


class TransientTransferError(TierError):
    """A transfer failed because its transport timed out or disconnected."""

    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class TransientSFTPError(TransientTransferError):
    """An SFTP batch failed because its SSH transport timed out or disconnected."""


class TransientSSHError(TransientTransferError):
    """A streamed SSH transfer failed because its transport disconnected."""


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
    compression: bool = False
    sftp_download_buffer_size_bytes: int = 262144
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
        if type(self.compression) is not bool:
            raise ValueError("compression must be a boolean")
        if (type(self.sftp_download_buffer_size_bytes) is not int
                or not 32768 <= self.sftp_download_buffer_size_bytes <= 262144):
            raise ValueError("SFTP download buffer size must be an integer between 32768 and 262144 bytes")
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
            control_template = str(self.control_path)
            if not self.control_path.is_absolute() or control_template.count("%C") != 1:
                raise ValueError("control_path must be absolute and contain exactly one %C token")
            expanded_control_path = control_template.replace("%C", "0" * 40)
            if len(os.fsencode(expanded_control_path)) > 80:
                raise ValueError("control_path is too long for a Unix socket; use a shorter path")
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

    def stream_remote_files(
        self, remote_paths: Sequence[str], targets: Sequence[tuple[Path, int | None]]
    ) -> None: ...


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
        if config.compression:
            self.options.extend(["-o", "Compression=yes"])

    def run_ssh(self, command: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                ["ssh", "-T", *self.options, self.target, command],
                text=True,
                capture_output=True,
                timeout=self.config.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise TransientSSHError("SSH operation timed out", retryable=False) from exc
        except OSError as exc:
            raise TransientSSHError("SSH process could not complete", retryable=False) from exc
        if result.returncode == 255:
            raise _ssh_transport_failure("SSH operation", result.stderr or "")
        if check and result.returncode != 0:
            raise TierError(f"remote SSH operation failed (exit {result.returncode})")
        return result

    def run_sftp(self, batch: str) -> subprocess.CompletedProcess[str]:
        commands = {line.strip().split(maxsplit=1)[0].lower() for line in batch.splitlines() if line.strip()}
        sftp_options = ["-P", str(self.config.port)]
        # Luna's SFTP server accepts 256 KiB read requests, but rejected larger
        # write packets. Only get-only batches use the tuned download buffer;
        # uploads and mixed batches keep OpenSSH's compatible default.
        if commands and commands <= {"get"}:
            sftp_options.extend(["-B", str(self.config.sftp_download_buffer_size_bytes)])
        sftp_options.extend(self.options[2:])
        try:
            result = subprocess.run(
                ["sftp", "-b", "-", *sftp_options, self.target],
                input=batch,
                text=True,
                capture_output=True,
                timeout=self.config.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise TransientSFTPError("SFTP operation timed out", retryable=False) from exc
        except OSError as exc:
            raise TransientSFTPError("SFTP process could not complete", retryable=False) from exc
        if result.returncode == 255:
            failure = _ssh_transport_failure("SFTP operation", result.stderr or "")
            if isinstance(failure, TransientSSHError):
                raise TransientSFTPError(str(failure))
            raise failure
        if result.returncode != 0:
            raise TierError(f"SFTP operation failed (exit {result.returncode})")
        return result

    def stream_remote_files(
        self, remote_paths: Sequence[str], targets: Sequence[tuple[Path, int | None]]
    ) -> None:
        """Stream concatenated remote files into private local staging files.

        Each target has an exact byte length, except an optional final target
        with a size of ``None``. That final manifest trailer is bounded so an
        unexpected remote response cannot fill the local filesystem.
        """
        if not remote_paths or len(remote_paths) != len(targets):
            raise TierError("SSH stream paths and targets do not match")
        if any(size is None for _path, size in targets[:-1]):
            raise TierError("only the final SSH stream target may have unknown size")
        if any(size is not None and (type(size) is not int or size < 0) for _path, size in targets):
            raise TierError("SSH stream target sizes must be nonnegative integers")

        command = "cat -- " + " ".join(shlex.quote(path) for path in remote_paths)
        process: subprocess.Popen[bytes] | None = None
        error_output = tempfile.TemporaryFile()
        deadline = time.monotonic() + self.config.timeout_seconds
        try:
            process = subprocess.Popen(
                ["ssh", "-T", *self.options, self.target, command],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=error_output,
                bufsize=0,
            )
            assert process.stdout is not None
            fd = process.stdout.fileno()
            os.set_blocking(fd, False)
            selector = selectors.DefaultSelector()
            selector.register(fd, selectors.EVENT_READ)
            target_index = 0
            target_remaining = targets[0][1]
            total_unknown = 0
            trailer_limit = MAX_MANIFEST_BYTES
            open_target: object | None = None

            def open_current():
                nonlocal open_target
                if open_target is None:
                    path, _size = targets[target_index]
                    path.parent.mkdir(parents=True, exist_ok=True)
                    open_target = path.open("xb")
                return open_target

            def advance_target() -> None:
                nonlocal target_index, target_remaining, open_target
                if open_target is not None:
                    open_target.flush()
                    os.fsync(open_target.fileno())
                    open_target.close()
                    open_target = None
                target_index += 1
                if target_index < len(targets):
                    target_remaining = targets[target_index][1]

            def advance_empty_targets() -> None:
                while target_index < len(targets) - 1 and target_remaining == 0:
                    open_current()
                    advance_target()

            advance_empty_targets()

            while True:
                remaining_time = deadline - time.monotonic()
                if remaining_time <= 0:
                    raise TimeoutError("SSH stream timed out")
                events = selector.select(min(remaining_time, 1.0))
                if not events:
                    if process.poll() is not None:
                        # Drain the pipe once after process exit; EOF is observed
                        # by os.read on the next readiness notification.
                        continue
                    continue
                chunk = os.read(fd, 64 * 1024)
                if not chunk:
                    break
                offset = 0
                while offset < len(chunk):
                    advance_empty_targets()
                    if target_index >= len(targets):
                        raise TierError("SSH stream returned unexpected trailing data")
                    if target_remaining is None:
                        take = len(chunk) - offset
                        total_unknown += take
                        if total_unknown > trailer_limit:
                            raise TierError("remote manifest exceeds the 1 MiB stream limit")
                    else:
                        take = min(len(chunk) - offset, target_remaining)
                    sink = open_current()
                    sink.write(chunk[offset:offset + take])
                    offset += take
                    if target_remaining is not None:
                        target_remaining -= take
                        if target_remaining == 0:
                            advance_target()
            return_code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
            if return_code == 255:
                error_output.seek(0)
                diagnostic = error_output.read().decode("utf-8", errors="replace")
                raise _ssh_transport_failure("SSH stream", diagnostic)
            if return_code != 0:
                raise TierError(f"SSH stream failed (exit {return_code})")
            if target_index < len(targets) - 1:
                if target_remaining:
                    raise TierError("SSH stream ended before a declared file was complete")
                raise TierError("SSH stream ended before all declared files were received")
            if target_index == len(targets) - 1:
                if target_remaining is not None:
                    raise TierError("SSH stream ended before a declared file was complete")
                if open_target is None:
                    open_current()
                advance_target()
        except TimeoutError as exc:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            raise TransientSSHError("SSH stream timed out", retryable=False) from exc
        except subprocess.TimeoutExpired as exc:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            raise TransientSSHError("SSH stream timed out", retryable=False) from exc
        except OSError as exc:
            raise TransientSSHError("SSH stream process could not complete", retryable=False) from exc
        finally:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                if process.stdout is not None:
                    process.stdout.close()
            if 'selector' in locals():
                selector.close()
            if 'open_target' in locals() and open_target is not None:
                open_target.close()
            error_output.close()


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
        except TransientTransferError:
            raise
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TransientSSHError("SSH operation could not complete", retryable=False) from exc
        if result.returncode == 255:
            raise _ssh_transport_failure("SSH operation", result.stderr or "")
        if check and result.returncode != 0:
            raise TierError(f"remote SSH operation failed (exit {result.returncode})")
        return result

    def _sftp(self, lines: list[str]) -> None:
        try:
            result = self.transport.run_sftp("\n".join(lines) + "\n")
        except TransientSFTPError:
            raise
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TransientSFTPError("SFTP connection failed", retryable=False) from exc
        if result.returncode == 255:
            failure = _ssh_transport_failure("SFTP operation", result.stderr or "")
            if isinstance(failure, TransientSSHError):
                raise TransientSFTPError(str(failure))
            raise failure
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
        quoted = shlex.quote(self._remote_path(path))
        result = self._ssh(
            f"if [ -L {quoted} ]; then exit 2; elif [ -e {quoted} ]; then exit 0; else exit 1; fi",
            check=False,
        )
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        raise TierError("could not query remote object state")

    def object_exists(self, key: RegionKey) -> bool:
        """Check for a region object without following symlinks."""
        return self._exists(self._object_path(key))

    def usage(self) -> int:
        self._ensure_root()
        with self._lock, self._remote_writer_lock():
            return self._usage_unlocked()

    def usage_snapshot(self) -> int:
        """Read objects and staging usage without creating or changing remote paths.

        This is an advisory, unlocked snapshot for planning. Every publish
        still rechecks quota while holding the remote writer lock.
        """
        root = shlex.quote(str(self.root))
        checks = [
            f"if [ -L {root} ]; then exit 2; fi; "
            f"if [ -e {root} ] && [ ! -d {root} ]; then exit 3; fi"
        ]
        for path in (self.objects, self.staging):
            quoted = shlex.quote(str(path))
            checks.append(
                f"if [ -L {quoted} ]; then exit 2; "
                f"elif [ -d {quoted} ]; then du -sb -- {quoted}; "
                f"elif [ -e {quoted} ]; then exit 3; fi"
            )
        result = self._ssh("; ".join(checks))
        try:
            return sum(int(line.split(maxsplit=1)[0]) for line in result.stdout.splitlines())
        except (IndexError, ValueError) as exc:
            raise TierError("remote storage usage response is invalid") from exc

    def _usage_unlocked(self) -> int:
        result = self._ssh(
            "du -sb -- " + shlex.quote(str(self.objects)) + " " + shlex.quote(str(self.staging))
        )
        try:
            return sum(int(line.split()[0]) for line in result.stdout.splitlines() if line.split())
        except ValueError as exc:
            raise TierError("remote storage usage response is invalid") from exc

    @staticmethod
    def _validate_manifest(key: RegionKey, manifest: object) -> dict:
        if not isinstance(manifest, dict) or manifest.get("format") != 1 or manifest.get("region") != {
            "dimension": key.dimension, "x": key.x, "z": key.z
        }:
            raise TierError(f"remote manifest identity mismatch for {key.name}")
        files = manifest.get("files")
        if not isinstance(files, dict) or f"region/r.{key.x}.{key.z}.mca" not in files:
            raise TierError(f"remote region bundle is incomplete for {key.name}")
        if len(files) > MAX_BUNDLE_FILES:
            raise TierError("remote region bundle has too many files")
        for member, info in files.items():
            if not DirectoryObjectStore._valid_member(key, member) or not isinstance(info, dict):
                raise TierError(f"invalid remote bundle member: {member}")
            if info.get("name") != PurePosixPath(member).name:
                raise TierError(f"invalid remote bundle filename: {member}")
            if type(info.get("size")) is not int or info["size"] < 0:
                raise TierError(f"invalid remote bundle size: {member}")
            if not re.fullmatch(r"[0-9a-f]{64}", str(info.get("sha256", ""))):
                raise TierError(f"invalid remote bundle checksum: {member}")
        return manifest

    def _manifest_bytes(self, key: RegionKey) -> bytes:
        remote = shlex.quote(self._remote_path(self._manifest_path(key)))
        try:
            # Read one byte beyond the limit so a hostile/corrupt remote
            # manifest cannot make subprocess.run buffer an unbounded stdout.
            result = self._ssh(f"head -c {MAX_MANIFEST_BYTES + 1} -- {remote}")
            encoded = result.stdout.encode("utf-8")
            if len(encoded) > MAX_MANIFEST_BYTES:
                raise TierError("remote manifest exceeds the 1 MiB size limit")
            return encoded
        except TransientTransferError:
            raise
        except (TierError, UnicodeError) as exc:
            raise TierError(f"remote manifest unavailable or corrupt for {key.name}") from exc

    def _manifest(self, key: RegionKey) -> tuple[dict, bytes]:
        try:
            encoded = self._manifest_bytes(key)
            manifest = json.loads(encoded)
        except (OSError, json.JSONDecodeError) as exc:
            raise TierError(f"remote manifest unavailable or corrupt for {key.name}") from exc
        return self._validate_manifest(key, manifest), encoded

    def read_manifest(self, key: RegionKey) -> dict:
        return self._manifest(key)[0]

    def read_manifest_with_hash(self, key: RegionKey) -> tuple[dict, str]:
        manifest, encoded = self._manifest(key)
        return manifest, hashlib.sha256(encoded).hexdigest()

    def manifest_hash(self, key: RegionKey) -> str:
        return hashlib.sha256(self._manifest_bytes(key)).hexdigest()

    def verify(self, key: RegionKey) -> dict:
        with self._lock, self._remote_writer_lock():
            return self._verify_unlocked(key)

    def _verify_unlocked(self, key: RegionKey) -> dict:
        manifest, encoded = self._manifest(key)
        with tempfile.TemporaryDirectory(prefix="fiesta-tier-verify-") as temporary:
            self._fetch_unlocked(key, Path(temporary), manifest, hashlib.sha256(encoded).hexdigest())
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
                if len(encoded) > MAX_MANIFEST_BYTES or len(manifest_files) > MAX_BUNDLE_FILES:
                    raise TierError("region bundle exceeds the supported manifest limits")
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
        with self._lock, self._remote_writer_lock():
            if expected_manifest is None:
                manifest, encoded = self._manifest(key)
                expected_manifest = manifest
                expected_manifest_hash = hashlib.sha256(encoded).hexdigest()
            else:
                self._validate_manifest(key, expected_manifest)
            return self._fetch_unlocked(key, destination, expected_manifest, expected_manifest_hash)

    def _fetch_unlocked(
        self,
        key: RegionKey,
        destination: Path,
        manifest: dict,
        expected_manifest_hash: str,
    ) -> dict:
        self._validate_manifest(key, manifest)
        if sum(int(info["size"]) for info in manifest["files"].values()) > self.max_bytes:
            raise TierError("remote bundle exceeds the configured storage quota")
        if destination.is_symlink():
            raise TierError(f"restore destination is a symlink: {destination}")
        destination.mkdir(parents=True, exist_ok=True)
        destination = destination.resolve(strict=True)
        object_path = self._object_path(key)
        members = sorted(manifest["files"].items())
        final_targets: list[tuple[str, dict, Path]] = []
        for member, info in members:
            relative = PurePosixPath(member)
            target = destination.joinpath(*relative.parts)
            parent = target.parent
            if parent.is_symlink() or target.is_symlink():
                raise TierError(f"restore destination contains a symlink: {member}")
            if target.exists():
                raise TierError(f"restore destination already contains a file: {member}")
            final_targets.append((member, info, target))

        last_transient_error: TransientTransferError | None = None
        for attempt in range(2):
            with tempfile.TemporaryDirectory(prefix=".fiesta-fetch-", dir=destination) as temporary:
                attempt_root = Path(temporary)
                staged_targets: list[tuple[str, dict, Path, Path]] = []
                stream_paths: list[str] = []
                stream_targets: list[tuple[Path, int | None]] = []
                for member, info, final_target in final_targets:
                    relative = PurePosixPath(member)
                    staged_target = attempt_root.joinpath(*relative.parts)
                    staged_target.parent.mkdir(parents=True, exist_ok=True)
                    stream_paths.append(self._remote_path(object_path / relative))
                    stream_targets.append((staged_target, int(info["size"])))
                    staged_targets.append((member, info, staged_target, final_target))
                manifest_after = attempt_root / "manifest.json"
                stream_paths.append(self._remote_path(self._manifest_path(key)))
                stream_targets.append((manifest_after, None))
                try:
                    self.transport.stream_remote_files(stream_paths, stream_targets)
                except TransientTransferError as exc:
                    last_transient_error = exc
                    if attempt == 0 and exc.retryable:
                        time.sleep(0.25)
                        continue
                    raise

                try:
                    encoded_after = manifest_after.read_bytes()
                    after = json.loads(encoded_after)
                except (OSError, json.JSONDecodeError) as exc:
                    raise TierError(f"remote manifest changed during restore for {key.name}") from exc
                if (hashlib.sha256(encoded_after).hexdigest() != expected_manifest_hash
                        or after != manifest):
                    raise TierError(f"remote manifest changed during restore for {key.name}")
                for member, info, staged_target, _final_target in staged_targets:
                    if staged_target.is_symlink() or not staged_target.is_file():
                        raise TierError(f"downloaded bundle member is not a regular file: {member}")
                    if (staged_target.stat().st_size != int(info["size"])
                            or self._hash(staged_target) != info["sha256"]):
                        raise TierError(f"download verification failed: {member}")
                    with staged_target.open("rb") as file:
                        os.fsync(file.fileno())

                for _member, _info, staged_target, final_target in staged_targets:
                    final_target.parent.mkdir(parents=True, exist_ok=True)
                    if final_target.is_symlink() or final_target.exists():
                        raise TierError(f"restore destination changed during restore: {final_target}")
                    os.replace(staged_target, final_target)
                    DirectoryObjectStore._fsync_dir(final_target.parent)
                break
        else:
            raise TierError("SSH restore failed after a transient disconnect") from last_transient_error
        return manifest

    def delete(
        self,
        key: RegionKey,
        *,
        expected_manifest_hash: str | None = None,
        expected_manifest: dict | None = None,
    ) -> None:
        if expected_manifest is not None and expected_manifest_hash is not None:
            self._validate_manifest(key, expected_manifest)
            if not re.fullmatch(r"[0-9a-f]{64}", expected_manifest_hash):
                raise TierError("invalid expected manifest checksum for remote cleanup")
            object_path = self._object_path(key)
            manifest_path = self._manifest_path(key)
            lock_path = self.lock_path
            lines = [
                "set -eu",
                "umask 077",
                f"lock_path={shlex.quote(str(lock_path))}",
                "if ! mkdir -- \"$lock_path\"; then exit 73; fi",
                "cleanup() { rmdir -- \"$lock_path\" >/dev/null 2>&1 || true; }",
                "trap cleanup EXIT",
                "trap 'exit 129' HUP",
                "trap 'exit 130' INT",
                "trap 'exit 143' TERM",
                f"manifest_path={shlex.quote(str(manifest_path))}",
                "actual=$(sha256sum -- \"$manifest_path\")",
                "actual=${actual%% *}",
                f"if [ \"$actual\" != {shlex.quote(expected_manifest_hash)} ]; then echo FIESTA_TIER_MANIFEST_MISMATCH >&2; exit 71; fi",
            ]
            lines.extend(
                f"rm -- {shlex.quote(self._remote_path(object_path / PurePosixPath(member)))}"
                for member in sorted(expected_manifest["files"])
            )
            directories = sorted(
                {
                    object_path / PurePosixPath(member).parent
                    for member in expected_manifest["files"]
                    if PurePosixPath(member).parent != PurePosixPath(".")
                },
                key=lambda path: len(path.parts),
                reverse=True,
            )
            lines.extend(
                f"rmdir -- {shlex.quote(self._remote_path(path))}"
                for path in directories
            )
            lines.extend([
                f"rm -- {shlex.quote(self._remote_path(manifest_path))}",
                f"rmdir -- {shlex.quote(self._remote_path(object_path))}",
            ])
            result = self._ssh("; ".join(lines), check=False)
            if result.returncode == 71 and "FIESTA_TIER_MANIFEST_MISMATCH" in result.stderr:
                raise TierError(f"remote manifest changed before cleanup for {key.name}")
            if result.returncode == 73:
                raise TierError("remote writer lock is already present; inspect it before cleanup")
            if result.returncode != 0:
                raise TierError(f"remote bundle cleanup failed (exit {result.returncode}); inspect remote state")
            return

        with self._lock, self._remote_writer_lock():
            manifest, encoded = self._manifest(key)
            if expected_manifest is not None and manifest != expected_manifest:
                raise TierError(f"remote manifest changed before cleanup for {key.name}")
            if (expected_manifest_hash is not None
                    and hashlib.sha256(encoded).hexdigest() != expected_manifest_hash):
                raise TierError(f"remote manifest changed before cleanup for {key.name}")
            object_path = self._object_path(key)
            lines = [
                f"rm -- {shlex.quote(self._remote_path(object_path / PurePosixPath(member)))}"
                for member in sorted(manifest["files"])
            ]
            lines.append(f"rm -- {shlex.quote(self._remote_path(object_path / 'manifest.json'))}")
            directories = sorted(
                {object_path / PurePosixPath(member).parent for member in manifest["files"] if PurePosixPath(member).parent != PurePosixPath(".")},
                key=lambda path: len(path.parts),
                reverse=True,
            )
            lines.extend(
                f"rmdir -- {shlex.quote(self._remote_path(path))}"
                for path in directories
            )
            lines.append(f"rmdir -- {shlex.quote(self._remote_path(object_path))}")
            self._ssh(" && ".join(lines))


__all__ = ["OpenSSHConfig", "OpenSSHObjectStore", "OpenSSHTransport", "SSHTransport"]
