from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fiesta_world_tier import OpenSSHConfig, OpenSSHObjectStore, RegionKey, TierError, TierManager
from fiesta_world_tier.openssh_store import (
    OpenSSHTransport,
    TransientSFTPError,
    TransientSSHError,
)
from fiesta_world_tier.store import RegionBundle


class FakeOpenSSH:
    """Small local SFTP/SSH fixture; it never contacts a network host."""

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir()
        self.path("/home/fiesta").mkdir(parents=True)
        self.available_bytes = 1_000_000_000_000
        self.sftp_batches: list[str] = []
        self.ssh_commands: list[str] = []
        self.stream_calls: list[tuple[list[str], list[tuple[Path, int | None]]]] = []

    def path(self, remote: str) -> Path:
        value = PurePosixPath(remote)
        if not value.is_absolute() or ".." in value.parts:
            raise AssertionError(f"unsafe test path: {remote}")
        return self.root.joinpath(*value.parts[1:])

    @staticmethod
    def result(code: int = 0, output: str = "", error: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], code, output, error)

    def run_ssh(self, command: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
        self.ssh_commands.append(command)
        output = ""
        if command.startswith("if [ -L ") and "df -B1 --output=avail -- " in command:
            match = re.match(r"if \[ -L (.*?) \];", command)
            fallback = re.search(r"else df -B1 --output=avail -- (.*?); fi$", command)
            if match is None or fallback is None:
                raise AssertionError("unexpected filesystem space query")
            remote_root = shlex.split(match.group(1))[0]
            root = self.path(remote_root)
            if root.is_symlink():
                return self.result(2, error="remote path is a symlink")
            if root.exists() and not root.is_dir():
                return self.result(3, error="remote path is not a directory")
            home = self.path(shlex.split(fallback.group(1))[0])
            if not home.exists():
                return self.result(1, error="remote home is missing")
            return self.result(output=f"Avail\n{self.available_bytes}\n")
        if command.startswith("set -eu; "):
            assignments = {}
            for part in command.split("; "):
                if part.startswith(("lock_path=", "manifest_path=")):
                    name, value = part.split("=", 1)
                    assignments[name] = shlex.split(value)[0]
            lock_remote = assignments["lock_path"]
            manifest_remote = assignments["manifest_path"]
            digest_match = re.search(r"!= ([0-9a-f]{64})", command)
            if digest_match is None:
                raise AssertionError("atomic cleanup command omitted its manifest checksum")
            lock_path = self.path(lock_remote)
            if lock_path.exists():
                return self.result(73, error="writer lock exists")
            lock_path.mkdir()
            try:
                manifest_path = self.path(manifest_remote)
                encoded = manifest_path.read_bytes()
                if hashlib.sha256(encoded).hexdigest() != digest_match.group(1):
                    return self.result(71, error="FIESTA_TIER_MANIFEST_MISMATCH")
                manifest = json.loads(encoded)
                object_remote = PurePosixPath(manifest_remote).parent
                for member in sorted(manifest["files"]):
                    self.path(str(object_remote / member)).unlink()
                directories = sorted(
                    {
                        PurePosixPath(member).parent
                        for member in manifest["files"]
                        if PurePosixPath(member).parent != PurePosixPath(".")
                    },
                    key=lambda path: len(path.parts),
                    reverse=True,
                )
                for directory in directories:
                    self.path(str(object_remote / directory)).rmdir()
                manifest_path.unlink()
                self.path(str(object_remote)).rmdir()
                return self.result()
            finally:
                lock_path.rmdir()
        exists_match = re.fullmatch(
            r"if \[ -L (.*?) \]; then exit 2; elif \[ -e (.*?) \]; then exit 0; else exit 1; fi",
            command,
        )
        if exists_match:
            symlink_path = self.path(shlex.split(exists_match.group(1))[0])
            target_path = self.path(shlex.split(exists_match.group(2))[0])
            if symlink_path.is_symlink():
                return self.result(2, error="remote path is a symlink")
            return self.result(0 if target_path.exists() else 1)
        if command.startswith("if [ -L "):
            checked = [shlex.split(value)[0] for value in re.findall(r"\[ -L (.*?) \]", command)]
            for remote in checked:
                path = self.path(remote)
                if path.is_symlink():
                    return self.result(2, error="remote storage path is a symlink")
                if path.exists() and not path.is_dir():
                    return self.result(3, error="remote storage path is not a directory")
            measured = re.findall(r"elif \[ -d (.*?) \]; then du -sb -- (.*?)\;", command)
            for checked_path, measured_path in measured:
                remote = shlex.split(measured_path)[0]
                if remote != shlex.split(checked_path)[0]:
                    raise AssertionError("usage check and measurement paths differ")
                path = self.path(remote)
                if path.is_dir():
                    total = sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
                    output += f"{total}\t{remote}\n"
            return self.result(0, output=output)
        try:
            for part in command.split(" && "):
                args = shlex.split(part)
                if not args:
                    continue
                if args[0] == "umask":
                    continue
                if args[0] == "mkdir":
                    recursive = "-p" in args
                    paths = [arg for arg in args[1:] if arg != "-p" and arg != "--"]
                    for value in paths:
                        path = self.path(value)
                        if recursive:
                            path.mkdir(parents=True, exist_ok=True)
                        else:
                            path.mkdir()
                elif args[0] == "chmod":
                    for value in args[2:]:
                        if value != "--":
                            self.path(value).chmod(int(args[1], 8))
                elif args[0] == "du":
                    for value in args[3:]:
                        path = self.path(value)
                        if not path.exists():
                            raise FileNotFoundError(value)
                        total = sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
                        output += f"{total}\t{value}\n"
                elif args[0] == "test":
                    if "--" in args:
                        raise AssertionError("shell test builtin does not accept -- here")
                    candidate = self.path(args[-1])
                    exists = candidate.is_dir() if "-d" in args else candidate.exists()
                    return self.result(0 if exists else 1)
                elif args[0] == "cat":
                    output += self.path(args[-1]).read_text(encoding="utf-8")
                elif args[0] == "head":
                    limit = int(args[2])
                    output += self.path(args[-1]).read_bytes()[:limit].decode("utf-8")
                elif args[0] == "rmdir":
                    self.path(args[-1]).rmdir()
                elif args[0] == "rm":
                    candidate = self.path(args[-1])
                    if "-rf" in args:
                        shutil.rmtree(candidate, ignore_errors=True)
                    else:
                        candidate.unlink()
                else:
                    raise AssertionError(f"unsupported SSH fixture command: {args[0]}")
        except FileExistsError as exc:
            result = self.result(1, error=str(exc))
        except Exception as exc:
            result = self.result(2, error=str(exc))
        else:
            result = self.result(0, output=output)
        if check and result.returncode:
            raise TierError("fixture SSH command failed")
        return result

    def run_sftp(self, batch: str) -> subprocess.CompletedProcess[str]:
        self.sftp_batches.append(batch)
        try:
            for line in batch.splitlines():
                args = shlex.split(line)
                command = args[0]
                if command == "mkdir":
                    self.path(args[1]).mkdir()
                elif command == "put":
                    local = Path(args[-2])
                    remote = self.path(args[-1])
                    remote.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(local, remote)
                elif command == "get":
                    remote = self.path(args[-2])
                    local = Path(args[-1])
                    local.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(remote, local)
                elif command == "rename":
                    old, new = self.path(args[1]), self.path(args[2])
                    if new.exists():
                        raise FileExistsError(new)
                    new.parent.mkdir(parents=True, exist_ok=True)
                    os.rename(old, new)
                elif command == "rm":
                    self.path(args[1]).unlink()
                elif command == "rmdir":
                    self.path(args[1]).rmdir()
                else:
                    raise AssertionError(f"unsupported SFTP fixture command: {command}")
        except Exception as exc:
            raise TierError("fixture SFTP command failed") from exc
        return self.result()

    def stream_remote_files(
        self, remote_paths: list[str], targets: list[tuple[Path, int | None]]
    ) -> None:
        self.stream_calls.append((list(remote_paths), list(targets)))
        payload = b"".join(self.path(remote).read_bytes() for remote in remote_paths)
        offset = 0
        for target, size in targets:
            if size is None:
                chunk = payload[offset:]
                offset = len(payload)
            else:
                chunk = payload[offset:offset + size]
                offset += len(chunk)
                if len(chunk) != size:
                    raise TierError("fixture stream was truncated")
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as output:
                output.write(chunk)
        if offset != len(payload):
            raise TierError("fixture stream returned trailing bytes")


class CorruptingOpenSSH(FakeOpenSSH):
    def run_sftp(self, batch: str) -> subprocess.CompletedProcess[str]:
        result = super().run_sftp(batch)
        for line in batch.splitlines():
            args = shlex.split(line)
            if args and args[0] == "put" and args[-1].endswith("r.-2.5.mca"):
                self.path(args[-1]).write_bytes(b"corrupted during transport")
                break
        return result


class InterruptingUploadOpenSSH(FakeOpenSSH):
    def run_sftp(self, batch: str) -> subprocess.CompletedProcess[str]:
        partial = []
        for line in batch.splitlines():
            partial.append(line)
            if shlex.split(line)[0] == "put":
                super().run_sftp("\n".join(partial) + "\n")
                raise TransientSFTPError("simulated disconnect after partial upload")
        return super().run_sftp(batch)


class TimedOutUploadOpenSSH(FakeOpenSSH):
    def run_sftp(self, batch: str) -> subprocess.CompletedProcess[str]:
        partial = []
        for line in batch.splitlines():
            partial.append(line)
            if shlex.split(line)[0] == "put":
                super().run_sftp("\n".join(partial) + "\n")
                raise TransientSFTPError("simulated SFTP operation timeout", retryable=False)
        return super().run_sftp(batch)


class MidFileDisconnectOpenSSH(FakeOpenSSH):
    def __init__(self, root: Path):
        super().__init__(root)
        self.partial_bytes = 0
        self.interrupted_file_bytes = 0

    def run_sftp(self, batch: str) -> subprocess.CompletedProcess[str]:
        self.sftp_batches.append(batch)
        for line in batch.splitlines():
            args = shlex.split(line)
            if args[0] == "mkdir":
                self.path(args[1]).mkdir()
                continue
            if args[0] != "put":
                raise AssertionError(f"unexpected fixture SFTP command before disconnect: {args[0]}")
            local = Path(args[-2])
            remote = self.path(args[-1])
            remote.parent.mkdir(parents=True, exist_ok=True)
            contents = local.read_bytes()
            self.interrupted_file_bytes = len(contents)
            self.partial_bytes = max(1, len(contents) // 2)
            remote.write_bytes(contents[:self.partial_bytes])
            raise TransientSFTPError("simulated SFTP disconnect mid-file")
        raise AssertionError("fixture SFTP batch did not start an upload")


class DisconnectingOpenSSH(FakeOpenSSH):
    def __init__(self, root: Path, get_disconnects: int, retryable: bool = True):
        super().__init__(root)
        self.get_disconnects = get_disconnects
        self.fail_fetch = False
        self.retryable = retryable

    def stream_remote_files(
        self, remote_paths: list[str], targets: list[tuple[Path, int | None]]
    ) -> None:
        super().stream_remote_files(remote_paths, targets)
        if self.fail_fetch and self.get_disconnects:
            self.get_disconnects -= 1
            raise TransientSSHError(
                "simulated SSH disconnect after partial stream", retryable=self.retryable
            )


class OpenSSHStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        identity = self.root / "id_ed25519"
        identity.write_text("fixture key path only", encoding="utf-8")
        identity.chmod(0o600)
        known_hosts = self.root / "known_hosts"
        known_hosts.write_text("fixture host key pin\n", encoding="utf-8")
        control_path = self.root / "control-%C"
        self.config = OpenSSHConfig(
            host="example.invalid",
            username="fiesta",
            port=22,
            identity_file=identity,
            known_hosts=known_hosts,
            remote_root=PurePosixPath("/home/fiesta/.local/share/fiesta-world-tier"),
            max_bytes=1_000_000,
            control_path=control_path,
        )
        self.transport = FakeOpenSSH(self.root / "remote")
        self.store = OpenSSHObjectStore(self.config, self.transport)
        self.key = RegionKey("overworld", -2, 5)
        self.source = self.root / "server"
        self.create_bundle()

    def tearDown(self):
        self.temp.cleanup()

    def create_bundle(self) -> dict[str, Path]:
        files: dict[str, Path] = {}
        for member, content in (
            ("region/r.-2.5.mca", b"terrain data"),
            ("entities/r.-2.5.mca", b"entity data"),
            ("poi/r.-2.5.mca", b"poi data"),
            ("region/c.-64.160.mcc", b"oversized chunk data"),
            ("entities/c.-64.160.mcc", b"oversized entity data"),
            ("poi/c.-64.160.mcc", b"oversized poi data"),
        ):
            path = self.source / member
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            files[member] = path
        return files

    def test_remote_bundle_round_trip_and_cleanup(self):
        files = self.create_bundle()
        generation, digest = self.store.publish(self.key, files)
        self.assertEqual(self.store.manifest_hash(self.key), digest)
        self.assertEqual(self.store.verify(self.key), generation)
        self.assertGreater(self.store.usage(), 0)

        restored = self.root / "restored"
        self.assertEqual(self.store.fetch(self.key, restored), generation)
        for member, source in files.items():
            self.assertEqual((restored / member).read_bytes(), source.read_bytes())

        self.transport.sftp_batches.clear()
        self.store.delete(self.key)
        self.assertEqual(self.transport.sftp_batches, [])
        self.assertEqual(self.store.usage(), 0)

    def test_publish_manifest_uses_bundle_member_name(self):
        files = self.create_bundle()
        member = "entities/r.-2.5.mca"
        renamed_source = self.root / "renamed-entity-source.tmp"
        renamed_source.write_bytes(files[member].read_bytes())
        files[member] = renamed_source

        manifest, digest = self.store.publish(self.key, files)

        self.assertEqual(manifest["files"][member]["name"], "r.-2.5.mca")
        self.assertEqual(self.store.verify(self.key), manifest)
        restored = self.root / "renamed-source-restore"
        self.assertEqual(
            self.store.fetch(self.key, restored, expected_manifest=manifest, expected_manifest_hash=digest),
            manifest,
        )
        self.assertEqual((restored / member).read_bytes(), renamed_source.read_bytes())

    def test_verified_fetch_batches_bundle_and_manifest_recheck(self):
        files = self.create_bundle()
        generation, digest = self.store.publish(self.key, files)
        manifest, manifest_hash = self.store.read_manifest_with_hash(self.key)
        self.assertEqual(manifest, generation)
        self.assertEqual(manifest_hash, digest)

        self.transport.sftp_batches.clear()
        destination = self.root / "verified-fetch"
        self.assertEqual(
            self.store.fetch(
                self.key,
                destination,
                expected_manifest=manifest,
                expected_manifest_hash=digest,
            ),
            generation,
        )
        self.assertEqual(len(self.transport.stream_calls), 1)
        self.assertTrue(self.transport.stream_calls[0][0][-1].endswith("manifest.json"))
        for member, source in files.items():
            self.assertEqual((destination / member).read_bytes(), source.read_bytes())

    def test_manifest_read_uses_ssh_stdout_instead_of_a_separate_sftp_transfer(self):
        generation, digest = self.store.publish(self.key, self.create_bundle())
        self.transport.sftp_batches.clear()
        self.transport.ssh_commands.clear()

        manifest, manifest_hash = self.store.read_manifest_with_hash(self.key)

        self.assertEqual(manifest, generation)
        self.assertEqual(manifest_hash, digest)
        self.assertEqual(self.transport.sftp_batches, [])
        self.assertTrue(self.transport.ssh_commands[-1].startswith("head -c "))

    def test_fetch_retries_once_after_transient_disconnect_in_private_staging(self):
        transport = DisconnectingOpenSSH(self.root / "retry-remote", get_disconnects=1)
        store = OpenSSHObjectStore(self.config, transport)
        generation, digest = store.publish(self.key, self.create_bundle())
        manifest, _ = store.read_manifest_with_hash(self.key)
        transport.fail_fetch = True
        transport.sftp_batches.clear()

        destination = self.root / "retry-fetch"
        self.assertEqual(
            store.fetch(
                self.key,
                destination,
                expected_manifest=manifest,
                expected_manifest_hash=digest,
            ),
            generation,
        )
        self.assertEqual(len(transport.stream_calls), 2)
        for member, source in self.create_bundle().items():
            self.assertEqual((destination / member).read_bytes(), source.read_bytes())
        self.assertTrue(store._exists(store._object_path(self.key)))

    def test_fetch_keeps_destination_empty_after_repeated_transient_disconnect(self):
        transport = DisconnectingOpenSSH(self.root / "retry-twice-remote", get_disconnects=2)
        store = OpenSSHObjectStore(self.config, transport)
        store.publish(self.key, self.create_bundle())
        manifest, digest = store.read_manifest_with_hash(self.key)
        transport.fail_fetch = True
        destination = self.root / "retry-twice-fetch"

        with self.assertRaisesRegex(TransientSSHError, "simulated SSH disconnect"):
            store.fetch(
                self.key,
                destination,
                expected_manifest=manifest,
                expected_manifest_hash=digest,
            )

        self.assertEqual(list(destination.iterdir()), [])
        self.assertTrue(store._exists(store._object_path(self.key)))

    def test_fetch_does_not_retry_nonretryable_transport_timeout(self):
        transport = DisconnectingOpenSSH(
            self.root / "timeout-remote", get_disconnects=1, retryable=False
        )
        store = OpenSSHObjectStore(self.config, transport)
        store.publish(self.key, self.create_bundle())
        manifest, digest = store.read_manifest_with_hash(self.key)
        transport.fail_fetch = True
        transport.sftp_batches.clear()
        destination = self.root / "timeout-fetch"

        with self.assertRaisesRegex(TransientSSHError, "simulated SSH disconnect"):
            store.fetch(
                self.key,
                destination,
                expected_manifest=manifest,
                expected_manifest_hash=digest,
            )

        self.assertEqual(
            len(transport.stream_calls),
            1,
        )
        self.assertEqual(list(destination.iterdir()), [])
        self.assertTrue(store._exists(store._object_path(self.key)))

    def test_cleanup_refuses_a_manifest_changed_after_restore(self):
        files = self.create_bundle()
        _generation, digest = self.store.publish(self.key, files)
        manifest_path = self.transport.path(str(self.store._manifest_path(self.key)))
        manifest_path.write_bytes(manifest_path.read_bytes() + b" ")

        with self.assertRaisesRegex(TierError, "manifest changed before cleanup"):
            self.store.delete(self.key, expected_manifest_hash=digest)
        self.assertTrue(self.transport.path(str(self.store._object_path(self.key) / "region/r.-2.5.mca")).is_file())

    def test_cleanup_with_known_manifest_uses_one_hash_guarded_remote_command(self):
        generation, digest = self.store.publish(self.key, self.create_bundle())
        self.transport.ssh_commands.clear()
        completed = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(self.transport, "run_ssh", return_value=completed) as run:
            self.store.delete(
                self.key,
                expected_manifest_hash=digest,
                expected_manifest=generation,
            )
        self.assertEqual(run.call_count, 1)
        command = run.call_args.args[0]
        self.assertIn("mkdir --", command)
        self.assertIn("sha256sum --", command)
        self.assertIn(digest, command)
        self.assertIn("trap cleanup EXIT", command)
        self.assertIn("rmdir --", command)
        self.assertEqual(self.transport.ssh_commands, [])

    def test_cleanup_known_manifest_refuses_remote_hash_mismatch(self):
        generation, digest = self.store.publish(self.key, self.create_bundle())
        completed = subprocess.CompletedProcess(
            [], 71, "", "FIESTA_TIER_MANIFEST_MISMATCH"
        )
        with patch.object(self.transport, "run_ssh", return_value=completed):
            with self.assertRaisesRegex(TierError, "manifest changed before cleanup"):
                self.store.delete(
                    self.key,
                    expected_manifest_hash=digest,
                    expected_manifest=generation,
                )

    def test_cleanup_known_manifest_keeps_remote_data_when_hash_changes(self):
        generation, digest = self.store.publish(self.key, self.create_bundle())
        manifest_path = self.transport.path(str(self.store._manifest_path(self.key)))
        manifest_path.write_bytes(manifest_path.read_bytes() + b" ")
        with self.assertRaisesRegex(TierError, "manifest changed before cleanup"):
            self.store.delete(
                self.key,
                expected_manifest_hash=digest,
                expected_manifest=generation,
            )
        self.assertTrue(self.store.object_exists(self.key))
        self.assertTrue(self.transport.path(str(self.store._object_path(self.key) / "region/r.-2.5.mca")).is_file())
        self.assertFalse(self.transport.path(str(self.store.lock_path)).exists())

    def test_cleanup_known_manifest_does_not_remove_an_existing_writer_lock(self):
        generation, digest = self.store.publish(self.key, self.create_bundle())
        lock_path = self.transport.path(str(self.store.lock_path))
        lock_path.mkdir()
        with self.assertRaisesRegex(TierError, "writer lock is already present"):
            self.store.delete(
                self.key,
                expected_manifest_hash=digest,
                expected_manifest=generation,
            )
        self.assertTrue(lock_path.is_dir())
        self.assertTrue(self.store.object_exists(self.key))

    def test_remote_writer_lock_retries_if_lock_disappears_between_checks(self):
        lock = shlex.quote(str(self.store.lock_path))
        mkdir_command = f"umask 077 && mkdir -- {lock}"
        test_command = f"test -d {lock}"
        rmdir_command = f"rmdir -- {lock}"
        mkdir_calls = 0

        def run(command, *, check=True):
            nonlocal mkdir_calls
            if command == mkdir_command:
                mkdir_calls += 1
                return self.transport.result(1 if mkdir_calls == 1 else 0)
            if command == test_command:
                return self.transport.result(1)
            if command == rmdir_command:
                return self.transport.result()
            raise AssertionError(f"unexpected SSH command: {command}")

        with patch.object(self.store, "_ssh", side_effect=run):
            with self.store._remote_writer_lock():
                pass

        self.assertEqual(mkdir_calls, 2)

    def test_usage_snapshot_is_advisory_and_does_not_create_remote_directories(self):
        self.assertEqual(self.store.usage_snapshot(), 0)
        self.assertFalse(self.transport.path(str(self.store.objects)).exists())
        self.assertFalse(self.transport.path(str(self.store.staging)).exists())

        files = self.create_bundle()
        self.store.publish(self.key, files)
        expected = self.store.usage()
        self.assertEqual(self.store.usage_snapshot(), expected)

    def test_available_snapshot_uses_existing_home_without_creating_tier_root(self):
        self.assertEqual(self.store.available_snapshot(), self.transport.available_bytes)
        self.assertFalse(self.transport.path(str(self.store.root)).exists())
        self.assertFalse(self.transport.path(str(self.store.objects)).exists())
        self.assertFalse(self.transport.path(str(self.store.staging)).exists())

    def test_usage_snapshot_rejects_a_symlinked_remote_root(self):
        remote_root = self.transport.path(str(self.store.root))
        remote_root.parent.mkdir(parents=True, exist_ok=True)
        remote_root.symlink_to(self.root)
        with self.assertRaisesRegex(TierError, "remote SSH operation failed"):
            self.store.usage_snapshot()

    def test_local_remote_size_matches_published_bundle_and_manifest(self):
        server = self.root / "size-server"
        for member, content in self.create_bundle().items():
            relative = Path(member)
            destination = server / "world" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content.read_bytes())

        @contextmanager
        def lease(_key):
            yield True

        manager = TierManager(server, self.root / "size.sqlite", self.store, lease)
        local_files = RegionBundle(server, self.key).files()
        expected_size = manager.local_region_remote_size(self.key)
        generation, _digest = self.store.publish(self.key, local_files)
        manifest, encoded = self.store._manifest(self.key)
        actual_size = sum(info["size"] for info in manifest["files"].values()) + len(encoded)
        self.assertEqual(generation, manifest)
        self.assertEqual(expected_size, actual_size)
        self.store.delete(self.key)

    def test_missing_remote_object_is_reported_as_absent(self):
        self.assertFalse(self.store._exists(self.store._object_path(self.key)))

    def test_sftp_uses_its_uppercase_port_option(self):
        transport = OpenSSHTransport(self.config)
        completed = subprocess.CompletedProcess([], 0, "", "")
        with patch("fiesta_world_tier.openssh_store.subprocess.run", return_value=completed) as run:
            transport.run_sftp("pwd\n")
        arguments = run.call_args.args[0]
        self.assertEqual(arguments[:5], ["sftp", "-b", "-", "-P", str(self.config.port)])
        self.assertIn("-R", arguments)
        self.assertIn("128", arguments)
        self.assertNotIn("-B", arguments)

    def test_sftp_request_window_can_be_configured(self):
        config = OpenSSHConfig(**{**self.config.__dict__, "sftp_max_requests": 64})
        transport = OpenSSHTransport(config)
        completed = subprocess.CompletedProcess([], 0, "", "")
        with patch("fiesta_world_tier.openssh_store.subprocess.run", return_value=completed) as run:
            transport.run_sftp("pwd\n")
        arguments = run.call_args.args[0]
        self.assertEqual(arguments[arguments.index("-R") + 1], "64")

    def test_sftp_uses_tuned_buffer_for_downloads_but_keeps_upload_default(self):
        transport = OpenSSHTransport(self.config)
        completed = subprocess.CompletedProcess([], 0, "", "")
        with patch("fiesta_world_tier.openssh_store.subprocess.run", return_value=completed) as run:
            transport.run_sftp('get "remote/file" "/tmp/file"\n')
            download_args = run.call_args.args[0]
            transport.run_sftp('put "/tmp/file" "remote/file"\n')
            upload_args = run.call_args.args[0]
            transport.run_sftp('get "remote/a" "/tmp/a"\nput "/tmp/b" "remote/b"\n')
            mixed_args = run.call_args.args[0]
        self.assertIn("-B", download_args)
        self.assertIn("262144", download_args)
        self.assertNotIn("-B", upload_args)
        self.assertNotIn("-B", mixed_args)
        for arguments in (download_args, upload_args, mixed_args):
            self.assertIn("-R", arguments)
            self.assertIn("128", arguments)

    def test_ssh_stream_splits_exact_file_lengths_and_manifest_trailer(self):
        fake_bin = self.root / "fake-bin"
        fake_bin.mkdir()
        fake_ssh = fake_bin / "ssh"
        fake_ssh.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys, time\n"
            "mode = os.environ.get('FIESTA_TEST_SSH_MODE', 'normal')\n"
            "if mode == 'timeout': time.sleep(2)\n"
            "elif mode == 'large': sys.stdout.buffer.write(b'x' * (1024 * 1024 + 1))\n"
            "elif mode == 'truncated': sys.stdout.buffer.write(b'abc')\n"
            "else: sys.stdout.buffer.write(b'abc{}')\n"
            "if mode == 'disconnect': print('ssh: connect to host 127.0.0.1 port 22: Connection refused', file=sys.stderr)\n"
            "sys.stdout.buffer.flush()\n"
            "sys.exit(255 if mode == 'disconnect' else 0)\n",
            encoding="utf-8",
        )
        fake_ssh.chmod(0o755)
        transport = OpenSSHTransport(self.config)
        empty = self.root / "stream-empty"
        region = self.root / "stream-region"
        manifest = self.root / "stream-manifest"
        with patch.dict(os.environ, {"PATH": f"{fake_bin}:{os.environ['PATH']}"}):
            transport.stream_remote_files(
                ["/remote/empty", "/remote/region", "/remote/manifest"],
                [(empty, 0), (region, 3), (manifest, None)],
            )
        self.assertEqual(empty.read_bytes(), b"")
        self.assertEqual(region.read_bytes(), b"abc")
        self.assertEqual(manifest.read_bytes(), b"{}")

    def test_ssh_stream_rejects_truncated_and_oversized_responses(self):
        fake_bin = self.root / "stream-error-bin"
        fake_bin.mkdir()
        fake_ssh = fake_bin / "ssh"
        fake_ssh.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "mode = os.environ.get('FIESTA_TEST_SSH_MODE')\n"
            "if mode == 'large': sys.stdout.buffer.write(b'x' * (1024 * 1024 + 1))\n"
            "else: sys.stdout.buffer.write(b'abc')\n"
            "if mode == 'disconnect': print('ssh: connect to host 127.0.0.1 port 22: Connection refused', file=sys.stderr)\n"
            "if mode == 'auth': print('Permission denied (publickey).', file=sys.stderr)\n"
            "sys.stdout.buffer.flush()\n"
            "sys.exit(255 if mode in ('disconnect', 'auth') else 0)\n",
            encoding="utf-8",
        )
        fake_ssh.chmod(0o755)
        transport = OpenSSHTransport(self.config)
        output = self.root / "stream-error-output"
        with patch.dict(os.environ, {"PATH": f"{fake_bin}:{os.environ['PATH']}", "FIESTA_TEST_SSH_MODE": "truncated"}):
            with self.assertRaisesRegex(TierError, "ended before a declared file"):
                transport.stream_remote_files(["/remote/a", "/remote/manifest"], [(output, 4), (self.root / "trailer", None)])
        self.assertEqual(output.read_bytes(), b"abc")
        with patch.dict(os.environ, {"PATH": f"{fake_bin}:{os.environ['PATH']}", "FIESTA_TEST_SSH_MODE": "large"}):
            with self.assertRaisesRegex(TierError, "exceeds the 1 MiB stream limit"):
                transport.stream_remote_files(["/remote/manifest"], [(self.root / "large-trailer", None)])
        with patch.dict(os.environ, {"PATH": f"{fake_bin}:{os.environ['PATH']}", "FIESTA_TEST_SSH_MODE": "disconnect"}):
            with self.assertRaisesRegex(TransientSSHError, "disconnected"):
                transport.stream_remote_files(["/remote/manifest"], [(self.root / "disconnected", None)])
        with patch.dict(os.environ, {"PATH": f"{fake_bin}:{os.environ['PATH']}", "FIESTA_TEST_SSH_MODE": "auth"}):
            with self.assertRaisesRegex(TierError, "exit 255") as raised:
                transport.stream_remote_files(["/remote/manifest"], [(self.root / "auth-failure", None)])
            self.assertNotIsInstance(raised.exception, TransientSSHError)

    def test_ssh_stream_preserves_local_enospc_when_partial_file_close_fails(self):
        fake_bin = self.root / "stream-enospc-bin"
        fake_bin.mkdir()
        fake_ssh = fake_bin / "ssh"
        fake_ssh.write_text(
            "#!/usr/bin/env python3\nimport sys\nsys.stdout.buffer.write(b'abc')\nsys.stdout.buffer.flush()\n",
            encoding="utf-8",
        )
        fake_ssh.chmod(0o755)
        output = self.root / "stream-enospc-output"
        original_open = Path.open

        class FullDiskSink:
            def __init__(self):
                self.close_called = False

            def write(self, _data):
                raise OSError(errno.ENOSPC, "No space left on device")

            def close(self):
                self.close_called = True
                raise OSError(errno.ENOSPC, "No space left on device")

        sink = FullDiskSink()

        def open_output(path, *args, **kwargs):
            if path == output:
                return sink
            return original_open(path, *args, **kwargs)

        transport = OpenSSHTransport(self.config)
        with patch.dict(os.environ, {"PATH": f"{fake_bin}:{os.environ['PATH']}"}):
            with patch.object(Path, "open", new=open_output):
                with self.assertRaisesRegex(TierError, "local disk is full while staging") as raised:
                    transport.stream_remote_files(["/remote/a"], [(output, 4)])
        self.assertIsInstance(raised.exception.__cause__, OSError)
        self.assertEqual(raised.exception.__cause__.errno, errno.ENOSPC)
        self.assertTrue(sink.close_called)

    def test_ssh_stream_timeout_is_not_retried(self):
        fake_bin = self.root / "stream-timeout-bin"
        fake_bin.mkdir()
        fake_ssh = fake_bin / "ssh"
        fake_ssh.write_text(
            "#!/usr/bin/env python3\nimport time\ntime.sleep(2)\n",
            encoding="utf-8",
        )
        fake_ssh.chmod(0o755)
        config = OpenSSHConfig(**{**self.config.__dict__, "timeout_seconds": 1})
        transport = OpenSSHTransport(config)
        with patch.dict(os.environ, {"PATH": f"{fake_bin}:{os.environ['PATH']}"}):
            with self.assertRaisesRegex(TransientSSHError, "timed out") as raised:
                transport.stream_remote_files(["/remote/manifest"], [(self.root / "timeout", None)])
        self.assertFalse(raised.exception.retryable)

    def test_sftp_process_timeout_is_not_retried(self):
        fake_bin = self.root / "sftp-timeout-bin"
        fake_bin.mkdir()
        fake_sftp = fake_bin / "sftp"
        fake_sftp.write_text(
            "#!/usr/bin/env python3\nimport time\ntime.sleep(2)\n",
            encoding="utf-8",
        )
        fake_sftp.chmod(0o755)
        config = OpenSSHConfig(**{**self.config.__dict__, "timeout_seconds": 1})
        transport = OpenSSHTransport(config)
        with patch.dict(os.environ, {"PATH": f"{fake_bin}:{os.environ['PATH']}"}):
            with self.assertRaisesRegex(TransientSFTPError, "timed out") as raised:
                transport.run_sftp("pwd\n")
        self.assertFalse(raised.exception.retryable)

    def test_config_rejects_unsupported_sftp_buffer_sizes(self):
        for value in (32767, 262145, True):
            with self.subTest(value=value):
                config = OpenSSHConfig(**{**self.config.__dict__, "sftp_download_buffer_size_bytes": value})
                with self.assertRaisesRegex(ValueError, "SFTP download buffer size"):
                    config.validate()

    def test_config_rejects_unsupported_sftp_request_windows(self):
        for value in (0, 129, True, 1.5):
            with self.subTest(value=value):
                config = OpenSSHConfig(**{**self.config.__dict__, "sftp_max_requests": value})
                with self.assertRaisesRegex(ValueError, "SFTP request window"):
                    config.validate()

    def test_transport_enables_private_connection_multiplexing(self):
        transport = OpenSSHTransport(self.config)
        completed = subprocess.CompletedProcess([], 0, "", "")
        with patch("fiesta_world_tier.openssh_store.subprocess.run", return_value=completed) as run:
            transport.run_ssh("true")
        arguments = run.call_args.args[0]
        self.assertIn("ControlMaster=auto", arguments)
        self.assertIn("ControlPersist=30", arguments)
        self.assertIn(f"ControlPath={self.config.control_path}", arguments)

    def test_ssh_transport_disconnect_is_retryable(self):
        transport = OpenSSHTransport(self.config)
        for diagnostic in (
            "connection refused",
            "Could not resolve hostname luna: Temporary failure in name resolution",
        ):
            with self.subTest(diagnostic=diagnostic):
                completed = subprocess.CompletedProcess([], 255, "", diagnostic)
                with patch("fiesta_world_tier.openssh_store.subprocess.run", return_value=completed):
                    with self.assertRaisesRegex(TransientSSHError, "transport disconnected") as raised:
                        transport.run_ssh("true")
                self.assertTrue(raised.exception.retryable)

    def test_ssh_authentication_and_host_key_errors_are_not_retryable(self):
        transport = OpenSSHTransport(self.config)
        for diagnostic in ("Permission denied (publickey).", "Host key verification failed."):
            with self.subTest(diagnostic=diagnostic):
                completed = subprocess.CompletedProcess([], 255, "", diagnostic)
                with patch("fiesta_world_tier.openssh_store.subprocess.run", return_value=completed):
                    with self.assertRaisesRegex(TierError, "exit 255") as raised:
                        transport.run_ssh("true")
                self.assertNotIsInstance(raised.exception, TransientSSHError)

    def test_manifest_read_preserves_retryable_transport_failure(self):
        with patch.object(self.transport, "run_ssh", side_effect=TransientSSHError("disconnected")):
            with self.assertRaisesRegex(TransientSSHError, "disconnected") as raised:
                self.store.read_manifest(self.key)
        self.assertTrue(raised.exception.retryable)

    def test_transport_enables_optional_ssh_compression_for_ssh_and_sftp(self):
        config = OpenSSHConfig(**{**self.config.__dict__, "compression": True})
        transport = OpenSSHTransport(config)
        completed = subprocess.CompletedProcess([], 0, "", "")
        with patch("fiesta_world_tier.openssh_store.subprocess.run", return_value=completed) as run:
            transport.run_ssh("true")
            transport.run_sftp("pwd\n")
        self.assertEqual(run.call_count, 2)
        for call in run.call_args_list:
            self.assertIn("Compression=yes", call.args[0])

    def test_config_rejects_non_boolean_compression(self):
        with self.assertRaisesRegex(ValueError, "compression must be a boolean"):
            OpenSSHConfig(**{**self.config.__dict__, "compression": 1}).validate()

    def test_journaled_manager_can_offload_and_restore_over_sftp_fixture(self):
        server = self.root / "world-server"
        key_root = server / "world"
        for member, content in (
            ("region/r.-2.5.mca", b"terrain data"),
            ("entities/r.-2.5.mca", b"entity data"),
            ("poi/r.-2.5.mca", b"poi data"),
            ("region/c.-64.160.mcc", b"oversized chunk data"),
            ("entities/c.-64.160.mcc", b"oversized entity data"),
            ("poi/c.-64.160.mcc", b"oversized poi data"),
        ):
            path = key_root / member
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

        @contextmanager
        def lease(_key):
            yield True

        manager = TierManager(
            server,
            self.root / "journal.sqlite",
            self.store,
            lease,
            local_free_reserve_bytes=0,
        )
        original = {
            member: (key_root / member).read_bytes()
            for member in (
                "region/r.-2.5.mca",
                "entities/r.-2.5.mca",
                "poi/r.-2.5.mca",
                "region/c.-64.160.mcc",
                "entities/c.-64.160.mcc",
                "poi/c.-64.160.mcc",
            )
        }
        manager.offload(self.key)
        self.assertEqual(manager.state(self.key), "REMOTE_ONLY")
        self.assertFalse((key_root / "region/r.-2.5.mca").exists())

        manager.restore(self.key)
        self.assertEqual(manager.state(self.key), "LOCAL_ONLY")
        self.assertEqual(
            {member: (key_root / member).read_bytes() for member in original},
            original,
        )
        self.assertEqual(self.store.usage(), 0)

    def test_quota_rejection_leaves_no_remote_object(self):
        tiny_config = OpenSSHConfig(
            **{**self.config.__dict__, "max_bytes": 1}
        )
        store = OpenSSHObjectStore(tiny_config, self.transport)
        with self.assertRaisesRegex(TierError, "quota"):
            store.publish(self.key, self.create_bundle())
        self.assertFalse(store._exists(store._object_path(self.key)))

    def test_staging_usage_counts_toward_remote_quota(self):
        config = OpenSSHConfig(**{**self.config.__dict__, "max_bytes": 1_000_000})
        transport = FakeOpenSSH(self.root / "staging-quota-remote")
        store = OpenSSHObjectStore(config, transport)
        files = self.create_bundle()
        store._ensure_root()

        staged_file = transport.path(str(store.staging / "interrupted-upload" / "region.mca"))
        staged_file.parent.mkdir(parents=True)
        staged_file.write_bytes(b"x" * (config.max_bytes - 1))

        with self.assertRaisesRegex(TierError, "quota"):
            store.publish(self.key, files)

        self.assertEqual(staged_file.stat().st_size, config.max_bytes - 1)
        self.assertFalse(store._exists(store._object_path(self.key)))
        self.assertTrue(all(path.is_file() for path in files.values()))

    def test_free_space_reserve_rejection_leaves_local_bundle_and_no_remote_object(self):
        config = OpenSSHConfig(**{**self.config.__dict__, "minimum_free_bytes": 1_000_000})
        transport = FakeOpenSSH(self.root / "reserve-remote")
        transport.available_bytes = 1_000_000
        store = OpenSSHObjectStore(config, transport)
        files = self.create_bundle()

        with self.assertRaisesRegex(TierError, "free-space reserve"):
            store.publish(self.key, files)

        self.assertFalse(store._exists(store._object_path(self.key)))
        self.assertEqual(list(transport.path(str(store.staging)).iterdir()), [])
        self.assertFalse(transport.path(str(store.lock_path)).exists())
        self.assertTrue(all(path.is_file() for path in files.values()))

    def test_free_space_reserve_allows_publish_with_headroom(self):
        config = OpenSSHConfig(**{**self.config.__dict__, "minimum_free_bytes": 100})
        transport = FakeOpenSSH(self.root / "reserve-boundary-remote")
        store = OpenSSHObjectStore(config, transport)
        files = self.create_bundle()
        transport.available_bytes = 100 + sum(path.stat().st_size for path in files.values()) + 1024

        store.publish(self.key, files)

        self.assertTrue(store.object_exists(self.key))

    def test_corrupt_remote_member_fails_checksum_verification(self):
        files = self.create_bundle()
        self.store.publish(self.key, files)
        remote = self.transport.path(str(self.store._object_path(self.key) / "region/r.-2.5.mca"))
        remote.write_bytes(b"corrupt data")
        with self.assertRaisesRegex(TierError, "download verification failed"):
            self.store.verify(self.key)

    def test_upload_is_verified_before_remote_publication(self):
        corrupting_transport = CorruptingOpenSSH(self.root / "corrupt-remote")
        store = OpenSSHObjectStore(self.config, corrupting_transport)
        with self.assertRaisesRegex(TierError, "remote upload verification failed"):
            store.publish(self.key, self.create_bundle())
        self.assertFalse(store._exists(store._object_path(self.key)))

    def test_interrupted_upload_cleans_staging_and_keeps_local_bundle(self):
        transport = InterruptingUploadOpenSSH(self.root / "interrupted-remote")
        store = OpenSSHObjectStore(self.config, transport)
        files = self.create_bundle()

        with self.assertRaisesRegex(TransientSFTPError, "partial upload"):
            store.publish(self.key, files)

        self.assertFalse(store.object_exists(self.key))
        self.assertEqual(list(transport.path(str(store.staging)).iterdir()), [])
        self.assertFalse(transport.path(str(store.lock_path)).exists())
        self.assertEqual(store.usage(), 0)
        self.assertTrue(all(path.is_file() for path in files.values()))

    def test_timed_out_upload_cleans_staging_and_keeps_local_bundle(self):
        transport = TimedOutUploadOpenSSH(self.root / "timed-out-remote")
        store = OpenSSHObjectStore(self.config, transport)
        files = self.create_bundle()

        with self.assertRaisesRegex(TransientSFTPError, "operation timeout") as raised:
            store.publish(self.key, files)

        self.assertFalse(raised.exception.retryable)
        self.assertFalse(store.object_exists(self.key))
        self.assertEqual(list(transport.path(str(store.staging)).iterdir()), [])
        self.assertFalse(transport.path(str(store.lock_path)).exists())
        self.assertEqual(store.usage(), 0)
        self.assertTrue(all(path.is_file() for path in files.values()))

    def test_mid_file_disconnect_cleans_partial_upload_and_keeps_local_bundle(self):
        transport = MidFileDisconnectOpenSSH(self.root / "mid-file-disconnect-remote")
        store = OpenSSHObjectStore(self.config, transport)
        files = self.create_bundle()

        with self.assertRaisesRegex(TransientSFTPError, "mid-file"):
            store.publish(self.key, files)

        self.assertGreater(transport.partial_bytes, 0)
        self.assertLess(transport.partial_bytes, transport.interrupted_file_bytes)
        self.assertFalse(store.object_exists(self.key))
        self.assertEqual(list(transport.path(str(store.staging)).iterdir()), [])
        self.assertFalse(transport.path(str(store.lock_path)).exists())
        self.assertEqual(store.usage(), 0)
        self.assertTrue(all(path.is_file() for path in files.values()))

    def test_config_rejects_unsafe_roots_and_insecure_identity_permissions(self):
        with self.assertRaisesRegex(ValueError, "beneath the SSH user's home"):
            OpenSSHConfig(**{**self.config.__dict__, "remote_root": PurePosixPath("/")}).validate()
        with self.assertRaisesRegex(ValueError, "simple path components"):
            OpenSSHConfig(**{**self.config.__dict__, "remote_root": PurePosixPath("/home/fiesta/../etc")}).validate()
        with self.assertRaisesRegex(ValueError, "beneath the SSH user's home"):
            OpenSSHConfig(**{**self.config.__dict__, "remote_root": PurePosixPath("/etc/world-tier")}).validate()
        self.config.identity_file.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "permissions"):
            self.config.validate()

    def test_config_rejects_invalid_minimum_free_bytes(self):
        for value in (-1, True, 1.5):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "minimum_free_bytes"):
                OpenSSHConfig(**{**self.config.__dict__, "minimum_free_bytes": value}).validate()

    def test_config_rejects_insecure_control_socket_directory(self):
        directory = self.root / "shared"
        directory.mkdir(mode=0o755)
        control_path = directory / "control-%C"
        with self.assertRaisesRegex(ValueError, "control_path parent permissions"):
            OpenSSHConfig(**{**self.config.__dict__, "control_path": control_path}).validate()

    def test_config_rejects_control_socket_paths_that_exceed_unix_socket_limit(self):
        directory = self.root / ("long-control-path-" * 4)
        directory.mkdir(mode=0o700)
        control_path = directory / "control-%C"
        with self.assertRaisesRegex(ValueError, "too long for a Unix socket"):
            OpenSSHConfig(**{**self.config.__dict__, "control_path": control_path}).validate()


if __name__ == "__main__":
    unittest.main()
