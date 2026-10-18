"""Process locks shared by the mail worker and local training commands."""

import fcntl
import hashlib
from contextlib import contextmanager
from pathlib import Path


class AlreadyRunning(RuntimeError):
    pass


@contextmanager
def process_lock(path, *, shared=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a") as handle:
        path.chmod(0o600)
        try:
            mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
            fcntl.flock(handle, mode | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AlreadyRunning(f"Another process holds {path.name}") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def account_key(account_id):
    return hashlib.sha256(account_id.lower().encode()).hexdigest()[:16]


@contextmanager
def worker_lock(db_path, account_id):
    path = Path(db_path).resolve()
    # The shared gate also respects older workers' exclusive database-wide lock.
    with (
        process_lock(path.with_suffix(".worker.lock"), shared=True),
        process_lock(path.with_suffix(f".{account_key(account_id)}.worker.lock")),
    ):
        yield
