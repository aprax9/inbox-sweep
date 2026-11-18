"""One inbox-sweep run per data directory at a time.

Two concurrent `apply` runs could double-trash and interleave undo logs, so
index/classify/plan/apply/undo take an exclusive lock file. A lock left behind
by a crashed run is detected and replaced:

- its process is gone (checked on POSIX and Windows, same machine only), or
- it can't be checked (another machine, unknown platform) and is over 12 hours old.

A lock whose process is still running is never taken over, however long the
run (a first Gmail index of a big mailbox can take most of a day), unless it is
over a week old, which only happens if the pid was reused.
"""

from __future__ import annotations

import json
import os
import socket
import time
from contextlib import contextmanager
from pathlib import Path

UNCHECKABLE_STALE_AFTER = 12 * 3600
ALIVE_STALE_AFTER = 7 * 86400


class LockError(RuntimeError):
    pass


def _pid_alive_windows(pid: int) -> bool | None:
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: it exists; anything else: gone
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return None
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None


def _pid_alive(pid: int) -> bool | None:
    """True/False if it can be checked, None if not.

    Never os.kill(pid, 0) on Windows: signal 0 there is CTRL_C_EVENT.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        return _pid_alive_windows(pid)
    if os.name != "posix":
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def _is_stale(path: Path, now: float | None = None) -> bool:
    try:
        info = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, json.JSONDecodeError):
        return True  # torn write by a crashed run
    now = time.time() if now is None else now
    age = now - float(info.get("at", 0) or 0)
    same_host = info.get("host", socket.gethostname()) == socket.gethostname()
    alive = _pid_alive(int(info.get("pid", 0) or 0)) if same_host else None
    if alive is False:
        return True
    if alive is True:
        return age > ALIVE_STALE_AFTER
    return age > UNCHECKABLE_STALE_AFTER


@contextmanager
def exclusive(data_dir: Path, command: str):
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / ".lock"
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if _is_stale(path):
                path.unlink(missing_ok=True)
                continue
            try:
                info = json.loads(path.read_text(encoding="utf-8") or "{}")
            except (OSError, json.JSONDecodeError):
                info = {}
            raise LockError(f"Another inbox-sweep run ({info.get('command', '?')}, pid {info.get('pid', '?')}) "
                            f"is using {data_dir}. If it isn't, delete {path}.")
        token = f"{os.getpid()}-{time.time_ns()}"
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"pid": os.getpid(), "host": socket.gethostname(), "command": command,
                       "at": time.time(), "token": token}, fh)
        try:
            yield
        finally:
            # Only remove the lock if it is still ours.
            try:
                if json.loads(path.read_text(encoding="utf-8") or "{}").get("token") == token:
                    path.unlink(missing_ok=True)
            except (OSError, json.JSONDecodeError):
                pass
        return
    raise LockError(f"Could not lock {data_dir}")
