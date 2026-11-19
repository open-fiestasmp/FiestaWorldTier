from __future__ import annotations

import fcntl
import os
import stat
from pathlib import Path

from .store import RegionKey, TierError


def paper_lease_is_held(dimension_root: Path, key: RegionKey) -> bool:
    """Verify Paper holds its process-level exclusive region lease.

    Java FileChannel locks and POSIX record locks use the same fcntl lock
    namespace on the supported Linux server. The probe must run in a child
    process because record locks are process-associated.
    """
    root = dimension_root.resolve(strict=True)
    if dimension_root.is_symlink() or not root.is_dir():
        raise TierError("Paper lease dimension root is not a safe directory")
    tier_root = root / ".world-tier"
    lease_root = tier_root / "paper-leases"
    for directory in (tier_root, lease_root):
        if directory.is_symlink() or not directory.is_dir():
            raise TierError("Paper region lease directory is missing or unsafe")
        if not directory.resolve(strict=True).is_relative_to(root):
            raise TierError("Paper region lease directory escapes the dimension root")

    path = lease_root / f"r.{key.x}.{key.z}.lock"
    if path.is_symlink() or not path.is_file():
        raise TierError("Paper does not hold the required region lease")
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise TierError("Paper region lease cannot be opened safely") from exc
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_uid != os.getuid():
            raise TierError("Paper region lease has an unexpected owner or type")
        if stat.S_IMODE(file_stat.st_mode) & 0o077:
            raise TierError("Paper region lease permissions must exclude group and other access")
        try:
            fcntl.lockf(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.lockf(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


__all__ = ["paper_lease_is_held"]
