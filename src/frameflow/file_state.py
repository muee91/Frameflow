"""Cheap local change detection with identical path/handle semantics.

This is a local binding guard, never a shared media identity. SHA-256 still owns
identity. All snapshots come from an open handle: Python 3.12 Windows path stat
and fstat can expose different meanings for ctime. Windows uses the documented
FILE_BASIC_INFO.ChangeTime explicitly; POSIX uses fstat's metadata-change time.
"""

from __future__ import annotations

import ctypes
import errno
from functools import lru_cache
import os
from pathlib import Path
import stat


class _FileBasicInfo(ctypes.Structure):
    _fields_ = [
        ("CreationTime", ctypes.c_int64),
        ("LastAccessTime", ctypes.c_int64),
        ("LastWriteTime", ctypes.c_int64),
        ("ChangeTime", ctypes.c_int64),
        ("FileAttributes", ctypes.c_uint32),
    ]


@lru_cache(maxsize=1)
def _windows_query():
    # Fixed-width types match Win32 on both 32- and 64-bit Python. The handle is
    # pointer-sized; allowing ctypes' default int would truncate 64-bit handles.
    query = ctypes.WinDLL("kernel32", use_last_error=True).GetFileInformationByHandleEx
    query.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    query.restype = ctypes.c_int
    return query


def _windows_times(descriptor: int) -> tuple[int, int]:
    import msvcrt

    info = _FileBasicInfo()
    handle = msvcrt.get_osfhandle(descriptor)
    if not _windows_query()(handle, 0, ctypes.byref(info), ctypes.sizeof(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    if info.ChangeTime <= 0 or info.LastWriteTime <= 0:
        # Do not silently substitute creation time or an incomplete signature
        # on a filesystem that cannot supply a usable change timestamp.
        raise OSError(errno.ENOTSUP, "The filesystem cannot report file change time")
    epoch = 116444736000000000  # FILETIME's 1601 epoch -> Unix epoch, in 100 ns.
    return (info.LastWriteTime - epoch) * 100, (info.ChangeTime - epoch) * 100


def descriptor_signature(descriptor: int) -> dict:
    """Return a versioned, JSON-safe snapshot without reading file contents."""
    value = os.fstat(descriptor)
    if not stat.S_ISREG(value.st_mode):
        raise OSError(errno.EINVAL, "Only regular files have media signatures")
    if os.name == "nt":
        modified, changed = _windows_times(descriptor)
        kind = "windows-change-time"
    else:
        modified, changed = value.st_mtime_ns, value.st_ctime_ns
        kind = "posix-ctime"
    return {
        "version": 1, "kind": kind,
        "device": value.st_dev, "inode": value.st_ino,
        "size": value.st_size, "modified_ns": modified, "changed_ns": changed,
    }


def path_signature(path: Path) -> dict:
    """Use the same handle query as hashing, refusing links and non-files."""
    path = Path(path)
    value = path.lstat()
    if stat.S_ISLNK(value.st_mode):
        raise OSError(errno.ELOOP, "Media links are not supported")
    if not stat.S_ISREG(value.st_mode):
        raise OSError(errno.EINVAL, "Only regular files have media signatures")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        signature = descriptor_signature(descriptor)
        # O_NOFOLLOW is not available on Windows. Recheck the entry after open
        # as well; callers compare a fresh path snapshot after any longer read.
        if not stat.S_ISREG(path.lstat().st_mode):
            raise OSError(errno.ELOOP, "The media entry changed while opening")
        return signature
    finally:
        os.close(descriptor)
