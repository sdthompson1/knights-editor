"""Cheap fingerprint of a file's on-disk state, for detecting external
modifications to files that are open in an editor."""

import os


def file_stamp(path: str) -> tuple[int, int] | None:
    """Return (mtime_ns, size) for `path`, or None if it can't be stat'ed
    (deleted, permission problem, ...).

    Editors record this after every read/write of their file and compare
    against a fresh stamp later: a difference means something else wrote
    the file in the meantime. mtime alone would miss writes within the
    filesystem's timestamp granularity; size alone would miss same-length
    edits; together they catch practically everything short of a
    same-size write in the same instant as ours.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)
