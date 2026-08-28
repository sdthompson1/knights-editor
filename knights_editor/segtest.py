"""Test Segment: run the Knights game on the segment being edited.

The editor copies the two template modules in segtest_template/ to a
temporary modules directory, filling in @@MARKERS@@ in their Lua, and
runs the game with

    knights -d <knights_data> -w -a --modules-dir <tmpdir>
            [--modules-dir <additional modules dir>]...
            --modules ktseditor_segtest_hook,<editor load order>,ktseditor_segtest

The hook module (loaded first) records every kts.LoadSegments call; the
test module (loaded last) picks out the segments file under test - with
the exact tile table the mod passed for it - and replaces the game menu
so that autostart drops straight into a one-segment dungeon.
"""

import os
import re
import shutil
import tempfile
from dataclasses import dataclass

from PySide6.QtCore import QObject, QProcess, Signal

from knights_editor.models import TileEntry, entry_walking

HOOK_MODULE = "ktseditor_segtest_hook"
TEST_MODULE = "ktseditor_segtest"

TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "segtest_template")

_MARKER_RE = re.compile(r"@@([A-Z_]+)@@")


def default_wall_tile(tile_table: dict[int, TileEntry]) -> int | None:
    """Tile number to surround the tested segment with.

    Prefers the lowest-numbered entry that is a single tile, blocked at
    walking level and whose Lua name contains "wall" (e.g. t_wall_normal);
    then any blocked single tile; then any blocked entry. None if the
    table has no blocked entry at all.
    """
    blocked = sorted(num for num, entry in tile_table.items()
                     if entry_walking(entry) == "blocked")
    if not blocked:
        return None
    single = [num for num in blocked if len(tile_table[num].tiles) == 1]
    for num in single:
        name = tile_table[num].tiles[0].name
        if name and "wall" in name.lower():
            return num
    return single[0] if single else blocked[0]


def find_owning_module(filepath: str,
                       mod_dirs: dict[str, str]) -> tuple[str, str] | None:
    """Which loaded mod a file belongs to.

    `mod_dirs` maps mod name to directory (see ModLoader.get_mod_dirs).
    Returns (mod name, path relative to the mod directory with "/"
    separators) for the mod whose directory contains `filepath`, the
    deepest such directory winning; None if no mod directory does.
    """
    target = os.path.realpath(filepath)
    best = None
    for name, mod_dir in mod_dirs.items():
        root = os.path.realpath(mod_dir)
        try:
            rel = os.path.relpath(target, root)
        except ValueError:  # different drives on Windows
            continue
        if rel.startswith(os.pardir) or os.path.isabs(rel):
            continue
        if best is None or len(root) > len(best[2]):
            best = (name, rel.replace(os.sep, "/"), root)
    return None if best is None else (best[0], best[1])


@dataclass
class SegtestParams:
    """Values substituted into the ktseditor_segtest template."""
    module_name: str        # mod owning the segments file
    segments_filename: str  # relative to that mod's directory
    segment_index: int      # 1-based, in file order
    wall_tile_index: int    # tile_table entry for the surrounding wall
    tile_table_module: str  # mod defining the fallback tile table ("" if none)
    tile_table_name: str    # global name of the fallback tile table

    def markers(self) -> dict[str, str]:
        return {
            "MODULE_NAME": self.module_name,
            "SEGMENTS_FILENAME": self.segments_filename,
            "SEGMENT_INDEX": str(self.segment_index),
            "WALL_TILE_INDEX": str(self.wall_tile_index),
            "TILE_TABLE_MODULE": self.tile_table_module,
            "TILE_TABLE_NAME": self.tile_table_name,
        }


def lua_escape(s: str) -> str:
    """Escape `s` for use inside a double-quoted Lua string literal."""
    return (s.replace("\\", "\\\\").replace('"', '\\"')
             .replace("\n", "\\n").replace("\r", "\\r"))


def substitute_markers(text: str, values: dict[str, str]) -> str:
    """Replace every @@NAME@@ in `text` with the (Lua-escaped) value.
    Raises KeyError for a marker with no value."""
    def repl(m):
        name = m.group(1)
        if name not in values:
            raise KeyError(f"template uses unknown marker @@{name}@@")
        return lua_escape(values[name])
    return _MARKER_RE.sub(repl, text)


def write_test_modules(dest_dir: str, params: SegtestParams):
    """Copy the template modules into `dest_dir` (a modules directory),
    filling in the markers in every .lua file."""
    values = params.markers()
    for module in (HOOK_MODULE, TEST_MODULE):
        src = os.path.join(TEMPLATE_DIR, module)
        dst = os.path.join(dest_dir, module)
        shutil.copytree(src, dst)
        for root, _dirs, files in os.walk(dst):
            for fname in files:
                if not fname.endswith(".lua"):
                    continue
                path = os.path.join(root, fname)
                with open(path, encoding="utf-8") as f:
                    text = f.read()
                new_text = substitute_markers(text, values)
                if new_text != text:
                    with open(path, "w", encoding="utf-8", newline="\n") as f:
                        f.write(new_text)


def build_command(binary: str, knights_data: str, modules_dir: str,
                  load_order: list[str], extra_modules_dirs=()) -> list[str]:
    """The command line for a test run. `modules_dir` is the directory
    holding the generated test modules; `load_order` is the editor's
    module load order, which the test modules are wrapped around;
    `extra_modules_dirs` are the modules directories the editor searched
    besides knights_data/modules (see mod_loader.game_modules_dirs: the
    edited mod's parent directory, then the configured additional
    directories), passed as further --modules-dir options in that order
    after the test modules directory, so the game resolves module names
    exactly as the editor did."""
    mods = [HOOK_MODULE] + [n for n in load_order
                            if n not in (HOOK_MODULE, TEST_MODULE)] + [TEST_MODULE]
    command = [binary, "-d", knights_data, "-w", "-a",
               "--modules-dir", modules_dir]
    for extra in extra_modules_dirs:
        command += ["--modules-dir", extra]
    return command + ["--modules", ",".join(mods)]


class SegmentTestRun(QObject):
    """One run of the game. Owns the temporary modules directory and
    removes it when the game exits.

    `finished(ok, message)` is emitted once: ok is False if the game
    could not be started or exited abnormally, and message then holds
    the details (including the game's output)."""

    finished = Signal(bool, str)

    # How much of the game's output to keep for error reports.
    _OUTPUT_TAIL = 4000

    def __init__(self, command: list[str], temp_dir: str, parent=None):
        super().__init__(parent)
        self.command = command
        self.temp_dir = temp_dir
        self._done = False
        self.process = QProcess(self)
        self.process.setProgram(command[0])
        self.process.setArguments(command[1:])
        self.process.setProcessChannelMode(
            QProcess.ProcessChannelMode.MergedChannels)
        self.process.finished.connect(self._on_finished)
        self.process.errorOccurred.connect(self._on_error)

    def start(self):
        self.process.start()

    def _output(self) -> str:
        data = bytes(self.process.readAllStandardOutput().data())
        text = data.decode("utf-8", errors="replace").strip()
        if len(text) > self._OUTPUT_TAIL:
            text = "..." + text[-self._OUTPUT_TAIL:]
        return text

    def _finish(self, ok: bool, message: str):
        if self._done:
            return
        self._done = True
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        self.finished.emit(ok, message)

    def _on_finished(self, exit_code: int, status):
        output = self._output()
        if status == QProcess.ExitStatus.CrashExit:
            self._finish(False, "Knights crashed.\n\n" + output)
        elif exit_code != 0:
            self._finish(False, f"Knights exited with code {exit_code}.\n\n"
                         + output)
        elif output.startswith("Usage:"):
            # The game prints its usage and exits 0 for an unknown
            # option: most likely an older build without --modules-dir
            # and --modules.
            self._finish(False, "Knights did not accept the command line. "
                         "The configured executable may be too old to "
                         "support the --modules-dir and --modules options "
                         "(see File > Settings).\n\n"
                         + " ".join(self.command) + "\n\n" + output)
        else:
            self._finish(True, output)

    def stop(self):
        """Kill the game if it is still running and remove the temporary
        modules directory. `finished` is not emitted (the caller asked
        for this). Safe to call more than once."""
        if self._done:
            return
        self._done = True  # makes any later _on_finished a no-op
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self.process.kill()
            self.process.waitForFinished(2000)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _on_error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self._finish(False, "Could not start Knights: "
                         f"'{self.command[0]}' was not found or is not "
                         "executable.")
        # Other errors (e.g. Crashed) are reported via finished().


def make_temp_modules_dir() -> str:
    return tempfile.mkdtemp(prefix="kts-editor-segtest-")
