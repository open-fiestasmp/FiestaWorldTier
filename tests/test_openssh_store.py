from __future__ import annotations

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
from fiesta_world_tier.openssh_store import OpenSSHTransport, TransientSFTPError
from fiesta_world_tier.store import RegionBundle


class FakeOpenSSH:
    """Small local SFTP/SSH fixture; it never contacts a network host."""

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir()
        self.sftp_batches: list[str] = []
        self.ssh_commands: list[str] = []

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


class CorruptingOpenSSH(FakeOpenSSH):
    def run_sftp(self, batch: str) -> subprocess.CompletedProcess[str]:
        result = super().run_sftp(batch)
        for line in batch.splitlines():
            args = shlex.split(line)
            if args and args[0] == "put" and args[-1].endswith("r.-2.5.mca"):
                self.path(args[-1]).write_bytes(b"corrupted during transport")
                break
        return result


class DisconnectingOpenSSH(FakeOpenSSH):
    def __init__(self, root: Path, get_disconnects: int, retryable: bool = True):
        super().__init__(root)
        self.get_disconnects = get_disconnects
        self.fail_fetch = False
        self.retryable = retryable

    def run_sftp(self, batch: str) -> subprocess.CompletedProcess[str]:
        is_fetch = self.fail_fetch and any(line.startswith("get ") for line in batch.splitlines())
        result = super().run_sftp(batch)
        if is_fetch and self.get_disconnects:
            self.get_disconnects -= 1
            raise TransientSFTPError(
                "simulated SSH disconnect after partial batch", retryable=self.retryable
            )
        return result


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
        self.assertEqual(len(self.transport.sftp_batches), 1)
        self.assertIn("manifest.json", self.transport.sftp_batches[0])
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
        self.assertTrue(self.transport.ssh_commands[-1].startswith("cat -- "))

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
        self.assertEqual(len(transport.sftp_batches), 2)
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

        with self.assertRaisesRegex(TransientSFTPError, "simulated SSH disconnect"):
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

        with self.assertRaisesRegex(TransientSFTPError, "simulated SSH disconnect"):
            store.fetch(
                self.key,
                destination,
                expected_manifest=manifest,
                expected_manifest_hash=digest,
            )

        self.assertEqual(
            sum(any(line.startswith("get ") for line in batch.splitlines()) for batch in transport.sftp_batches),
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

    def test_usage_snapshot_is_advisory_and_does_not_create_remote_directories(self):
        self.assertEqual(self.store.usage_snapshot(), 0)
        self.assertFalse(self.transport.path(str(self.store.objects)).exists())
        self.assertFalse(self.transport.path(str(self.store.staging)).exists())

        files = self.create_bundle()
        self.store.publish(self.key, files)
        expected = self.store.usage()
        self.assertEqual(self.store.usage_snapshot(), expected)

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

    def test_transport_enables_private_connection_multiplexing(self):
        transport = OpenSSHTransport(self.config)
        completed = subprocess.CompletedProcess([], 0, "", "")
        with patch("fiesta_world_tier.openssh_store.subprocess.run", return_value=completed) as run:
            transport.run_ssh("true")
        arguments = run.call_args.args[0]
        self.assertIn("ControlMaster=auto", arguments)
        self.assertIn("ControlPersist=30", arguments)
        self.assertIn(f"ControlPath={self.config.control_path}", arguments)

    def test_journaled_manager_can_offload_and_restore_over_sftp_fixture(self):
        server = self.root / "world-server"
        key_root = server / "world"
        for member, content in (
            ("region/r.-2.5.mca", b"terrain data"),
            ("entities/r.-2.5.mca", b"entity data"),
            ("poi/r.-2.5.mca", b"poi data"),
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
            for member in ("region/r.-2.5.mca", "entities/r.-2.5.mca", "poi/r.-2.5.mca")
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

    def test_corrupt_remote_member_fails_checksum_verification(self):
        files = self.create_bundle()
        self.store.publish(self.key, files)
        remote = self.transport.path(str(self.store._object_path(self.key) / "region/r.-2.5.mca"))
        remote.write_bytes(b"corrupt")
        with self.assertRaisesRegex(TierError, "download verification failed"):
            self.store.verify(self.key)

    def test_upload_is_verified_before_remote_publication(self):
        corrupting_transport = CorruptingOpenSSH(self.root / "corrupt-remote")
        store = OpenSSHObjectStore(self.config, corrupting_transport)
        with self.assertRaisesRegex(TierError, "remote upload verification failed"):
            store.publish(self.key, self.create_bundle())
        self.assertFalse(store._exists(store._object_path(self.key)))

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

    def test_config_rejects_insecure_control_socket_directory(self):
        directory = self.root / "shared"
        directory.mkdir(mode=0o755)
        control_path = directory / "control-%C"
        with self.assertRaisesRegex(ValueError, "control_path parent permissions"):
            OpenSSHConfig(**{**self.config.__dict__, "control_path": control_path}).validate()


if __name__ == "__main__":
    unittest.main()
