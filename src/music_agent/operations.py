"""Exclusive pipeline operations without holding the worker-thread state writer."""

from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock, Timeout

from .store import WriterLocked


@contextmanager
def lock(root: Path):
    directory = Path(root).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    mutex = FileLock(str(directory / 'operation.lock'), is_singleton=True)
    try:
        mutex.acquire(timeout=0)
    except Timeout as error:
        raise WriterLocked('another pipeline operation is active; resume after it finishes') from error
    try:
        yield
    finally:
        mutex.release()
