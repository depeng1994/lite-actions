"""Coordinate Worker job admission with the local automatic updater."""

import fcntl
from contextlib import contextmanager


@contextmanager
def activity_lock(ready_file, *, create=False):
    path = ready_file.with_name(ready_file.name + ".activity.lock")
    if create:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        stream = path.open("ab" if create else "rb")
    except FileNotFoundError:
        if create:
            raise
        # Releases predating the activity lock can still be upgraded.
        yield None
        return
    with stream:
        yield stream


def try_lock(stream):
    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True
