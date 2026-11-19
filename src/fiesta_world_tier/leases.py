from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .store import RegionKey, TierError


class FileRegionLease:
    """Cross-process lock for one region operation.

    This lock only serializes tier commands. It does not prove that Paper has
    unloaded a region or drained its read/write queues; an eviction caller must
    provide that Paper-side guarantee separately.
    """

    def __init__(self, server_root: Path):
        self.server_root = server_root.resolve()
        self.lock_root = self.server_root / ".world-tier" / "leases"

    @contextmanager
    def acquire(self, key: RegionKey) -> Iterator[bool]:
        base = self.server_root / ".world-tier"
        for directory in (base, self.lock_root):
            if directory.is_symlink():
                raise TierError(f"world-tier lease directory is a symlink: {directory}")
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not directory.is_dir() or not directory.resolve().is_relative_to(self.server_root):
                raise TierError(f"unsafe world-tier lease directory: {directory}")
        path = self.lock_root / f"{key.dimension}-r.{key.x}.{key.z}.lock"
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags, 0o600)
        except OSError as exc:
            raise TierError(f"could not open world-tier lease for {key.name}") from exc
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield True
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


__all__ = ["FileRegionLease"]
