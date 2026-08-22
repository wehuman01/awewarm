"""One cross-process lock for local awewarm state transactions.

Atomic file replacement prevents torn JSON, but it cannot prevent two
processes from reading the same state and later overwriting each other's
changes. The console entry point holds this lock for each short-lived command;
the resident server has its own in-process lock and does not use this one.
"""
import errno
import os
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path

from .config import config_path, state_path


class LockBusy(Exception):
    """Another awewarm process owns the local state transaction."""


def lock_path():
    return config_path().with_name("awewarm.lock")


def local_lock_paths():
    """Deterministic lock set for processes sharing config or state."""
    return sorted({
        config_path().with_name("awewarm.lock"),
        state_path().with_name("awewarm.lock"),
    })


def _try_lock(handle):
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise LockBusy()
            raise
        return

    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise LockBusy()


def _unlock(handle):
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def process_lock(path=None, timeout_seconds=5):
    """Hold the local transaction lock, waiting up to timeout_seconds."""
    path = Path(path or lock_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    if os.name == "nt" and path.stat().st_size == 0:
        handle.write(b"\0")
        handle.flush()
    try:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        deadline = time.monotonic() + max(0, timeout_seconds)
        while True:
            try:
                _try_lock(handle)
                break
            except LockBusy:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        try:
            yield
        finally:
            _unlock(handle)
    finally:
        handle.close()


@contextmanager
def local_process_lock(timeout_seconds=5):
    """Lock both local persistence roots within one total wait budget."""
    deadline = time.monotonic() + max(0, timeout_seconds)
    with ExitStack() as stack:
        for path in local_lock_paths():
            remaining = max(0, deadline - time.monotonic())
            stack.enter_context(process_lock(path, timeout_seconds=remaining))
        yield
