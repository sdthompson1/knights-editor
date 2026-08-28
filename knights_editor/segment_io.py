"""Read and write Knights segment (.txt) files.

The game reads these files as a stream of whitespace-separated tokens, so
it does not care how the numbers are split across lines. The editor is
deliberately stricter: it only accepts files laid out one row of the
tile grid per line (as the game's own data files and this module's
writer produce), and refuses anything else with a SegmentFormatError
rather than guessing. Guessing wrongly would silently corrupt the file
on the next save.
"""

import io

from knights_editor.models import Segment, Switch, Room
from knights_editor.line_endings import PLATFORM_DEFAULT, read_text_preserving
from knights_editor.text_files import write_text_atomic


class SegmentFormatError(ValueError):
    """The file is not laid out as a segment file the editor can edit.
    The message names the file and line number."""


class NotSegmentFileError(SegmentFormatError):
    """The file does not look like a segment file at all (its first
    real line is not "segment"), as opposed to a segment file with a
    problem in it."""


def load_segments(filepath: str) -> list[Segment]:
    """Load all segments from a .txt file. Raises SegmentFormatError if
    the file is not a well-formed segment file (see module docstring)."""
    return load_segments_with_line_ending(filepath)[0]


def load_segments_with_line_ending(filepath: str) -> tuple[list[Segment], str]:
    """Load all segments from a .txt file, also returning the file's
    line-ending style so it can be passed back to save_segments. Raises
    SegmentFormatError if the file is not a well-formed segment file."""
    text, line_ending = read_text_preserving(filepath)
    return _Parser(filepath, text.splitlines()).parse_file(), line_ending


class _Parser:
    """Line-oriented parser for one segment file. `self.i` is the index
    of the next unread line; error messages use 1-based line numbers."""

    def __init__(self, filepath: str, lines: list[str]):
        self.filepath = filepath
        self.lines = lines
        self.i = 0

    def error(self, message: str, line_no: int | None = None) -> SegmentFormatError:
        if line_no is None:
            line_no = self.i
        return SegmentFormatError(f"{self.filepath}, line {line_no}: {message}")

    def next_line(self, what: str) -> str:
        """Consume and return the next line (stripped), or raise if the
        file ends first."""
        if self.i >= len(self.lines):
            raise self.error(f"unexpected end of file: expected {what}", len(self.lines) + 1)
        line = self.lines[self.i].strip()
        self.i += 1
        return line

    @staticmethod
    def is_skippable(line: str) -> bool:
        return line == "" or line.startswith("#")

    def parse_int(self, text: str, what: str, minimum: int | None = None) -> int:
        try:
            value = int(text)
        except ValueError:
            raise self.error(f"expected {what} (an integer), got {text!r}")
        if minimum is not None and value < minimum:
            raise self.error(f"{what} must be at least {minimum}, got {value}")
        return value

    def parse_int_row(self, line: str, count: int, what: str) -> list[int]:
        """Parse a line of exactly `count` integers."""
        vals = line.split()
        if len(vals) != count:
            raise self.error(f"expected {count} {what} on this line, found {len(vals)}")
        return [self.parse_int(v, what) for v in vals]

    def parse_file(self) -> list[Segment]:
        segments: list[Segment] = []
        while self.i < len(self.lines):
            line = self.next_line("segment")
            if self.is_skippable(line):
                continue
            if line == "segment":
                seg = self.parse_segment()
                seg.source_file = self.filepath
                seg.source_index = len(segments)
                segments.append(seg)
            elif line == "eof":
                # The game stops reading here; so do we.
                break
            elif not segments:
                raise NotSegmentFileError(
                    f"{self.filepath}, line {self.i}: not a segment file "
                    f"(expected \"segment\", got {line!r})")
            else:
                raise self.error(f"expected \"segment\" or \"eof\", got {line!r}")
        return segments

    def parse_segment(self) -> Segment:
        """Parse one segment; called with the "segment" line consumed."""
        start_line = self.i
        name = ""
        width: int | None = None
        height: int | None = None
        data: list[list[int]] | None = None
        rooms: list[Room] | None = None
        switches: list[Switch] | None = None

        while True:
            line = self.next_line(f"\"end\" of the segment starting at line {start_line}")
            if self.is_skippable(line):
                continue
            if line == "end":
                break
            elif line.startswith("name:"):
                name = line[5:].strip()
            elif line.startswith("width:"):
                if data is not None:
                    raise self.error("\"width:\" must come before \"data:\"")
                width = self.parse_int(line[6:].strip(), "width", 1)
            elif line.startswith("height:"):
                if data is not None:
                    raise self.error("\"height:\" must come before \"data:\"")
                height = self.parse_int(line[7:].strip(), "height", 1)
            elif line == "data:":
                if data is not None:
                    raise self.error("segment has more than one \"data:\" section")
                if width is None or height is None:
                    raise self.error("\"width:\" and \"height:\" must come before \"data:\"")
                data = [self.parse_int_row(self.next_line(f"row {r + 1} of the tile data"),
                                           width, "tile numbers")
                        for r in range(height)]
            elif line == "rooms:":
                if rooms is not None:
                    raise self.error("segment has more than one \"rooms:\" section")
                count = self.parse_int(self.next_line("the number of rooms"),
                                       "number of rooms", 0)
                rooms = [Room(*self.parse_int_row(self.next_line(f"room {n + 1}"),
                                                  4, "room values (x y w h)"))
                         for n in range(count)]
            elif line == "switches:":
                if switches is not None:
                    raise self.error("segment has more than one \"switches:\" section")
                count = self.parse_int(self.next_line("the number of switches"),
                                       "number of switches", 0)
                switches = []
                for n in range(count):
                    raw = self.next_line(f"switch {n + 1}")
                    try:
                        switches.append(Switch.parse(raw))
                    except ValueError as e:
                        raise self.error(f"bad switch line: {e}")
            else:
                raise self.error(f"unexpected line in segment: {line!r}")

        if data is None:
            raise self.error(f"segment starting at line {start_line} has no \"data:\" section",
                             self.i)
        return Segment(name, width, height, data, rooms or [], switches or [])


def save_segments(filepath: str, segments: list[Segment],
                  line_ending: str = PLATFORM_DEFAULT) -> None:
    """Save segments to a .txt file using the given line-ending style
    (as returned by load_segments_with_line_ending); a freshly created
    file gets the platform default. The file is replaced atomically (see
    text_files.write_text_atomic)."""
    f = io.StringIO()
    for seg in segments:
        _write_segment(f, seg)
        f.write("\n")
    # The Knights segment loader expects the file to be terminated by
    # an "eof" marker after the final segment's "end".
    f.write("eof\n")
    write_text_atomic(filepath, f.getvalue().replace("\n", line_ending))


def _write_segment(f, seg: Segment) -> None:
    """Write a single segment."""
    f.write("segment\n")
    f.write(f"name: {seg.name}\n")
    f.write(f"width: {seg.width}\n")
    f.write(f"height: {seg.height}\n")
    f.write("data:\n")

    for row in seg.data:
        f.write(" ".join(f"{v:2d}" for v in row) + " \n")

    if seg.switches:
        f.write("switches:\n")
        f.write(f"{len(seg.switches)}\n")
        for sw in seg.switches:
            f.write(sw.raw_text + "\n")

    f.write("rooms:\n")
    f.write(f"{len(seg.rooms)}\n")
    for room in seg.rooms:
        f.write(f"{room.x} {room.y} {room.w} {room.h} \n")

    f.write("end\n")
