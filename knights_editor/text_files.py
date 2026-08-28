"""Reading and writing the editor's text files (Lua, segments, ...).

All files are treated as UTF-8 regardless of the platform's locale
encoding, which is what the game effectively expects (it reads raw
bytes, and the shipped data is UTF-8). A file that is not valid UTF-8
raises a NotUtf8Error naming the file and line.

Writes are atomic: the content goes to a temporary file in the same
directory which then replaces the target, so a failure part-way through
(disk full, encoding error, ...) leaves the original file intact instead
of truncated.
"""

import os
import shutil
import tempfile

ENCODING = "utf-8"


class NotUtf8Error(ValueError):
    """The file is not valid UTF-8 text."""

    def __init__(self, filepath: str, error: UnicodeDecodeError, line: int):
        self.filepath = filepath
        self.line = line
        byte = error.object[error.start]
        # Without the path, for messages that already name the file.
        self.reason = (f"not valid UTF-8 text (byte 0x{byte:02X} at "
                       f"offset {error.start}, line {line})")
        # With it, in "path:line:" form so Lua-style error locations
        # (see mod_loader.parse_lua_error) pick it up.
        super().__init__(f"{filepath}:{line}: {self.reason}")


def read_bytes_as_text(filepath: str) -> str:
    """Read a file as UTF-8, with all line endings left as they are in
    the file. Raises NotUtf8Error if it is not valid UTF-8."""
    with open(filepath, "rb") as f:
        raw = f.read()
    try:
        return raw.decode(ENCODING)
    except UnicodeDecodeError as e:
        line = raw.count(b"\n", 0, e.start) + 1
        raise NotUtf8Error(filepath, e, line) from None


def write_text_atomic(filepath: str, text: str) -> None:
    """Write `text` to `filepath` as UTF-8, replacing the file atomically.
    Line endings in `text` are written exactly as given (no translation).
    If `filepath` is a symlink, the file it points to is replaced."""
    target = os.path.realpath(filepath)
    directory = os.path.dirname(target) or "."
    data = text.encode(ENCODING)
    fd, tmp_path = tempfile.mkstemp(prefix=".kts-editor-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(target):
            # Keep the original file's permissions rather than mkstemp's
            # private 0600.
            shutil.copymode(target, tmp_path)
        os.replace(tmp_path, target)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
