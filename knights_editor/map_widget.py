"""Map canvas widget for displaying and editing segments."""

import copy
from enum import Enum, auto

from PySide6.QtWidgets import QWidget, QAbstractScrollArea
from PySide6.QtGui import QPainter, QColor, QPen, QBrush
from PySide6.QtCore import Qt, Signal

from knights_editor.models import Segment, TileEntry, Room, Switch, MapBlock
from knights_editor.graphics_cache import TilePixmapCache, TILE_SIZE
from knights_editor.room_check import rooms_overlap, RoomProblem, ProblemKind, problems_at
from knights_editor.zooming import next_level
from knights_editor.tile_palette_widget import BRUSH_BUTTONS


SCALE = 2  # Draw tiles at 2x size (at 100% zoom)

# The dungeon generator surrounds every segment with a one-tile wall ring,
# and room rectangles normally extend onto that ring (room x == 0, or
# x + w - 1 == width + 1). The ring is drawn around the segment so that
# rooms can be placed on it; it is only clickable in Rooms mode.
RING = 1

# Ring colours: taken from base/gfx/wall.bmp.
RING_BASE_COLOR = QColor(136, 136, 85)
RING_HATCH_COLOR = QColor(170, 170, 119)

# Undo snapshots kept per map (each is a deep copy of the segment).
MAX_UNDO = 200

# The block most recently cut or copied in Select mode. Shared by every
# map, so that it can be pasted into another segment or another file.
_clipboard: MapBlock | None = None


def clipboard_block() -> MapBlock | None:
    """The block that a paste would insert, or None if nothing has been
    cut or copied yet."""
    return _clipboard


class EditMode(Enum):
    SELECT = auto()
    PAINT = auto()
    SWITCHES = auto()
    ROOMS = auto()


# Rectangle in data tile coords: (col, row, w, h)
Rect = tuple[int, int, int, int]


class MapWidget(QWidget):
    """Widget for displaying and editing a map segment."""

    tile_picked = Signal(int, int)  # tile_number, mouse_button (BRUSH_BUTTONS value)
    tile_hovered = Signal(int)      # tile_number under mouse (-1 if none)
    # Room being dragged out or hovered in Rooms mode, as room coords
    # (x, y, w, h); w == 0 means none.
    room_hovered = Signal(int, int, int, int)
    # Room check problems under the mouse in Rooms mode (while they are
    # shown on the map): their messages joined by "; ", or "" if none.
    problem_hovered = Signal(str)
    switch_hovered = Signal(int)    # switch index under mouse in Switches mode (-1 if none)
    switch_selected = Signal(int)   # switch index (-1 if none)
    target_picked = Signal(int, int)  # col, row of picked target tile
    # The rectangle selected (or being dragged out) in Select mode, as
    # data tile coords (col, row, w, h); w == 0 means none.
    selection_changed = Signal(int, int, int, int)
    # Whether a paste is waiting for its click (see start_paste).
    pasting_changed = Signal(bool)
    segment_modified = Signal()
    zoom_changed = Signal(float)    # zoom factor (1.0 = 100%)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.segment: Segment | None = None
        self.tile_table: dict[int, TileEntry] = {}
        self.tile_cache: TilePixmapCache | None = None
        self.zoom = 1.0
        self.edit_mode = EditMode.SELECT
        self.brush_tiles: dict[int, int] = {}  # button -> tile_number
        self._mouse_pressed: int | None = None  # button currently held
        self._hover_pos: tuple[int, int] | None = None
        self._drag_start: tuple[int, int] | None = None
        self._drag_end: tuple[int, int] | None = None

        # Switch selection and target picking
        self._selected_switch: int = -1  # index into segment.switches
        # Index into the selected switch's funcs of the function highlighted
        # in the editor's function list (-1 = none); drawn in white.
        self.highlighted_func: int = -1
        self._picking_target: bool = False  # True when waiting for target click
        # True between a press that completed a target pick and the
        # doubleClick event Qt may deliver for that same press pair.
        self._pick_just_finished: bool = False
        # Index of the selected switch's func whose target circle is being
        # dragged to a new tile (-1 = none), and whether the undo snapshot
        # for this drag has been pushed yet.
        self._dragging_func: int = -1
        self._drag_undo_pushed: bool = False
        # Whether the undo snapshot for the current paint stroke (press
        # to release) has been pushed; one snapshot covers the whole stroke.
        self._paint_undo_pushed: bool = False

        # Select mode: the selected rectangle (None if nothing is
        # selected), and whether the clipboard block is following the
        # mouse waiting to be stamped down by a click (see start_paste).
        self._selection: Rect | None = None
        self._pasting: bool = False

        # Room check results, drawn (and reported on hover) in Rooms mode
        # when show_problems is set. See set_problems.
        self.problems: list[RoomProblem] = []
        self.show_problems = False

        # Undo/redo
        self._undo_stack: list[Segment] = []
        self._redo_stack: list[Segment] = []

        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def set_segment(self, segment: Segment | None):
        self.segment = segment
        self._selected_switch = -1
        self._undo_stack.clear()
        self._redo_stack.clear()
        self.cancel_paste()
        self.clear_selection()
        self._update_size()
        self.update()

    def set_problems(self, problems: list[RoomProblem], show: bool):
        """Set the room check problems to mark on the map (in Rooms mode)
        and whether to show them. Refreshes the hover report, since the
        problems under a stationary mouse may have changed."""
        changed = problems != self.problems or show != self.show_problems
        self.problems = problems
        self.show_problems = show
        self._emit_hover_info()
        # The markers are only drawn in Rooms mode (switching mode
        # repaints anyway), so e.g. a paint stroke, which re-runs the
        # check per tile, needn't repaint the whole map each time.
        if changed and self.edit_mode == EditMode.ROOMS:
            self.update()

    def set_tile_table(self, tile_table: dict[int, TileEntry],
                       tile_cache: TilePixmapCache):
        self.tile_table = tile_table
        self.tile_cache = tile_cache
        self.update()

    def _tile_px(self) -> int:
        """Size in pixels of one tile at the current zoom."""
        return max(1, round(TILE_SIZE * SCALE * self.zoom))

    def set_zoom(self, zoom: float):
        if zoom != self.zoom:
            self.zoom = zoom
            self._update_size()
            self.update()
            self.zoom_changed.emit(zoom)

    def zoom_in(self):
        self.set_zoom(next_level(self.zoom, +1))

    def zoom_out(self):
        self.set_zoom(next_level(self.zoom, -1))

    def zoom_reset(self):
        self.set_zoom(1.0)

    def _update_size(self):
        if self.segment:
            s = self._tile_px()
            w = (self.segment.width + 2 * RING) * s
            h = (self.segment.height + 2 * RING) * s
            self.setMinimumSize(w, h)
            self.setMaximumSize(w, h)
            self.resize(w, h)
        else:
            self.setMinimumSize(100, 100)

    def _push_undo(self):
        if self.segment:
            self._undo_stack.append(copy.deepcopy(self.segment))
            del self._undo_stack[:-MAX_UNDO]
            self._redo_stack.clear()

    def undo(self):
        if self._undo_stack and self.segment:
            self._redo_stack.append(copy.deepcopy(self.segment))
            self._restore_snapshot(self._undo_stack.pop())

    def redo(self):
        if self._redo_stack and self.segment:
            self._undo_stack.append(copy.deepcopy(self.segment))
            self._restore_snapshot(self._redo_stack.pop())

    def resize_segment(self, resized: Segment):
        """Replace the current segment's contents with `resized` - a copy
        of it at a different size (see Segment.resized) - as an undoable
        edit. The widget takes on the new size."""
        if not self.segment:
            return
        self._push_undo()
        self._restore_snapshot(resized)

    def _restore_snapshot(self, restored: Segment):
        """Replace the current segment's contents (including its size)
        with a snapshot.

        The switch list is swapped wholesale, so the selected-switch index
        may no longer refer to the same switch (or to any switch at all).
        Re-resolve the selection by switch position and re-emit
        switch_selected so the editor's switch panel is rebuilt from the
        restored data rather than showing stale function rows. Any
        in-progress target pick is cancelled, since the switch it was for
        may no longer exist.
        """
        switches = self.segment.switches
        sel_pos = None
        if 0 <= self._selected_switch < len(switches):
            sel = switches[self._selected_switch]
            sel_pos = (sel.x, sel.y)
        self.segment.data = restored.data
        self.segment.rooms = restored.rooms
        self.segment.switches = restored.switches
        if (restored.width, restored.height) != (self.segment.width, self.segment.height):
            self.segment.width = restored.width
            self.segment.height = restored.height
            # The mouse may now be off the segment (it was over a tile that
            # no longer exists); it is re-resolved on the next mouse move.
            self._hover_pos = None
            self._update_size()
            # The selection may now stick out of the segment
            self._set_selection(self._clip_to_map(self._selection))
        self._selected_switch = next(
            (i for i, s in enumerate(restored.switches)
             if (s.x, s.y) == sel_pos), -1)
        if self._picking_target:
            self.cancel_target_pick()
        self.switch_selected.emit(self._selected_switch)
        self.update()
        self.segment_modified.emit()
        self._emit_hover_info()

    def _tile_at(self, pos, include_ring: bool = False) -> tuple[int, int] | None:
        """Convert widget pixel position to data tile (col, row).

        With include_ring, positions on the outer wall ring are returned
        too, as col/row values of -RING or width/height."""
        if not self.segment:
            return None
        s = self._tile_px()
        col = pos.x() // s - RING
        row = pos.y() // s - RING
        margin = RING if include_ring else 0
        if (-margin <= col < self.segment.width + margin
                and -margin <= row < self.segment.height + margin):
            return (col, row)
        return None

    def _on_ring(self, col: int, row: int) -> bool:
        """True if (col, row) lies on the outer wall ring rather than the data."""
        seg = self.segment
        return not (0 <= col < seg.width and 0 <= row < seg.height)

    def _hovered_room_index(self, seg: Segment) -> int:
        """Return the index of the room under the mouse, or -1."""
        if not self._hover_pos:
            return -1
        col, row = self._hover_pos
        # Convert data coords to room coords
        rc, rr = col + 1, row + 1
        for i, room in enumerate(seg.rooms):
            if room.x <= rc < room.x + room.w and room.y <= rr < room.y + room.h:
                return i
        return -1

    def _drag_rect(self) -> Rect | None:
        """The rectangle currently being dragged out (Rooms and Select
        modes), in data tile coords, or None."""
        if not (self._drag_start and self._drag_end):
            return None
        x1 = min(self._drag_start[0], self._drag_end[0])
        y1 = min(self._drag_start[1], self._drag_end[1])
        x2 = max(self._drag_start[0], self._drag_end[0])
        y2 = max(self._drag_start[1], self._drag_end[1])
        return (x1, y1, x2 - x1 + 1, y2 - y1 + 1)

    def _drag_room(self) -> Room | None:
        """The room rectangle currently being dragged out, or None."""
        rect = self._drag_rect()
        if rect is None:
            return None
        x, y, w, h = rect
        # Convert data coords to room coords (+1 offset)
        return Room(x + 1, y + 1, w, h)

    # --- Select mode: selection, cut/copy/paste ---

    @property
    def selection(self) -> Rect | None:
        """The selected rectangle in data tile coords (col, row, w, h),
        which may extend onto the wall ring, or None."""
        return self._selection

    @property
    def pasting(self) -> bool:
        """True while the clipboard block follows the mouse, waiting for
        a click to stamp it down (see start_paste)."""
        return self._pasting

    def _clip_to_map(self, rect: Rect | None) -> Rect | None:
        """`rect` cut down to the segment plus its wall ring; None if
        nothing is left (or there is no segment)."""
        if rect is None or not self.segment:
            return None
        x, y, w, h = rect
        x1 = max(x, -RING)
        y1 = max(y, -RING)
        x2 = min(x + w, self.segment.width + RING)
        y2 = min(y + h, self.segment.height + RING)
        if x1 >= x2 or y1 >= y2:
            return None
        return (x1, y1, x2 - x1, y2 - y1)

    def _set_selection(self, rect: Rect | None):
        if rect != self._selection:
            self._selection = rect
            self.update()
        # Reported even if unchanged: the caller may be ending a drag,
        # during which the drag rectangle was reported instead.
        self._emit_hover_info()

    def clear_selection(self):
        self._set_selection(None)

    def copy_selection(self) -> bool:
        """Copy the selected rectangle (tiles, switches and the rooms
        entirely inside it) to the clipboard. Returns False if there is
        no selection."""
        global _clipboard
        if not self.segment or self._selection is None:
            return False
        _clipboard = self.segment.extract(*self._selection)
        return True

    def cut_selection(self, fill: int) -> bool:
        """Copy the selected rectangle to the clipboard, then clear it
        (see clear_selection_contents)."""
        if not self.copy_selection():
            return False
        self.clear_selection_contents(fill)
        return True

    def clear_selection_contents(self, fill: int) -> bool:
        """Set the selected rectangle's tiles to `fill` and delete the
        switches on them and the rooms entirely inside it, as an undoable
        edit. The selection itself stays. Returns False if there is no
        selection."""
        if not self.segment or self._selection is None:
            return False
        self._push_undo()
        self.segment.clear_rect(*self._selection, fill)
        self._selected_switch = -1
        self.update()
        self.segment_modified.emit()
        return True

    def start_paste(self) -> bool:
        """Begin a paste: the clipboard block follows the mouse (drawn
        translucently, its top-left tile at the pointer) until a left
        click stamps it down, or a right click or Escape cancels. Only
        meaningful in Select mode. Returns False if the clipboard is
        empty or there is no segment."""
        if _clipboard is None or not self.segment:
            return False
        if not self._pasting:
            self._pasting = True
            self.setCursor(Qt.CursorShape.CrossCursor)
            self.pasting_changed.emit(True)
            self.update()
        return True

    def cancel_paste(self):
        if self._pasting:
            self._pasting = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self.pasting_changed.emit(False)
            self.update()

    def _stamp(self, col: int, row: int):
        """Finish a paste by stamping the clipboard block down with its
        top-left tile at (col, row). The pasted area becomes the
        selection."""
        block = _clipboard
        if block is None or not self.segment:
            self.cancel_paste()
            return
        self._push_undo()
        # As when dragging out a room, existing rooms that the pasted
        # ones would overlap give way to them.
        self.segment.paste(block, col, row, room_conflict=rooms_overlap)
        self._selected_switch = -1
        self.cancel_paste()
        self._set_selection(
            self._clip_to_map((col, row, block.width, block.height)))
        self.update()
        self.segment_modified.emit()

    def _emit_hover_info(self):
        """Report what is under the mouse for the current edit mode: the
        room being dragged out (preferred) or hovered, plus any shown room
        check problems, in Rooms mode; or the hovered switch in Switches
        mode."""
        if self.edit_mode == EditMode.ROOMS:
            room = None
            problems: list[RoomProblem] = []
            if self.segment:
                room = self._drag_room()
                if room is None:
                    idx = self._hovered_room_index(self.segment)
                    if idx >= 0:
                        room = self.segment.rooms[idx]
                    # No problem reports mid-drag: the drag rectangle is
                    # the more useful information then.
                    if self.show_problems and self._hover_pos:
                        problems = problems_at(self.problems, *self._hover_pos)
            if room is None:
                self.room_hovered.emit(0, 0, 0, 0)
            else:
                self.room_hovered.emit(room.x, room.y, room.w, room.h)
            self.problem_hovered.emit("; ".join(p.message for p in problems))
        elif self.edit_mode == EditMode.SWITCHES:
            self.switch_hovered.emit(self._hovered_switch_index())
        elif self.edit_mode == EditMode.SELECT:
            # The rectangle being dragged out takes precedence over the
            # existing selection (which it is about to replace)
            rect = self._drag_rect() or self._selection
            self.selection_changed.emit(*(rect or (0, 0, 0, 0)))

    def _switch_at(self, col: int, row: int) -> int:
        """Return the index of the switch at (col, row), or -1."""
        if not self.segment:
            return -1
        for i, sw in enumerate(self.segment.switches):
            if sw.x == col and sw.y == row:
                return i
        return -1

    def _hovered_switch_index(self) -> int:
        """Return the index of the switch under the mouse, or -1."""
        if not self._hover_pos or not self.segment:
            return -1
        return self._switch_at(*self._hover_pos)

    def _func_target_at(self, sw: Switch, col: int, row: int) -> int:
        """Index of a func of `sw` whose target tile is (col, row), or -1.

        Prefers the func highlighted in the editor's function list, so that
        stacked targets can be dragged apart individually."""
        hf = self.highlighted_func
        if 0 <= hf < len(sw.funcs):
            f = sw.funcs[hf]
            if f.has_target and (sw.x + f.dx, sw.y + f.dy) == (col, row):
                return hf
        for i, f in enumerate(sw.funcs):
            if f.has_target and (sw.x + f.dx, sw.y + f.dy) == (col, row):
                return i
        return -1

    def _draw_switch_connections(self, painter: QPainter, sw: Switch, s: int,
                                 highlight_idx: int = -1):
        """Draw arrows from a switch to its target tiles.

        The function at index `highlight_idx` (if any) is drawn in white."""
        func_colors = {
            "toggle": QColor(255, 255, 0, 200),    # yellow
            "open":   QColor(0, 255, 100, 200),     # green
            "close":  QColor(255, 140, 0, 200),     # dark orange
            "shoot":  QColor(255, 60, 60, 200),     # red
        }
        dir_arrows = {
            "north": (0, -1), "east": (1, 0),
            "south": (0, 1), "west": (-1, 0),
        }

        sx = sw.x * s + s // 2  # switch center pixel
        sy = sw.y * s + s // 2

        custom_row = 0  # stacking counter for custom-func labels
        for fi, func in enumerate(sw.funcs):
            if fi == highlight_idx:
                color = QColor(255, 255, 255, 230)
            else:
                color = func_colors.get(func.name, QColor(200, 200, 200, 200))

            if not func.has_target:
                # Custom function: args are not a target offset, so draw
                # it on the switch tile itself (grey circle + name)
                painter.setBrush(QColor(color.red(), color.green(),
                                        color.blue(), 80))
                painter.setPen(QPen(color, 1))
                painter.drawEllipse(sx - s // 3, sy - s // 3,
                                    s * 2 // 3, s * 2 // 3)
                painter.setPen(color)
                painter.drawText(sx + s // 2 + 3,
                                 sy + custom_row * painter.fontMetrics().height(),
                                 func.name)
                custom_row += 1
                continue

            tx = (sw.x + func.dx) * s + s // 2  # target center pixel
            ty = (sw.y + func.dy) * s + s // 2

            # Draw line from switch to target
            painter.setPen(QPen(color, 2))
            painter.drawLine(sx, sy, tx, ty)

            # Draw target marker
            painter.setBrush(QColor(color.red(), color.green(), color.blue(), 80))
            painter.setPen(QPen(color, 1))
            painter.drawEllipse(tx - s // 3, ty - s // 3, s * 2 // 3, s * 2 // 3)

            # For shoot, draw a direction arrow at the target
            if func.name == "shoot" and func.direction:
                adx, ady = dir_arrows.get(func.direction, (0, 0))
                # Shaft
                ex = tx + adx * s
                ey = ty + ady * s
                painter.setPen(QPen(color, 3))
                painter.drawLine(tx, ty, ex, ey)
                # Arrowhead: two lines angled ~30 degrees back from the tip
                import math
                angle = math.radians(30)
                cos_a, sin_a = math.cos(angle), math.sin(angle)
                # Rotate the negative-shaft direction by +/- 30 degrees
                bx, by = -adx, -ady  # unit back direction
                head = s // 3
                lx = bx * cos_a - by * sin_a
                ly = bx * sin_a + by * cos_a
                rx = bx * cos_a + by * sin_a
                ry = -bx * sin_a + by * cos_a
                painter.drawLine(ex, ey, int(ex + lx * head), int(ey + ly * head))
                painter.drawLine(ex, ey, int(ex + rx * head), int(ey + ry * head))

            # Draw label near the midpoint
            mx = (sx + tx) // 2
            my = (sy + ty) // 2
            painter.setPen(color)
            painter.drawText(mx + 3, my - 3, func.name)

    def start_target_pick(self):
        """Enter target-picking mode. Next left-click emits target_picked."""
        self._picking_target = True
        self.setCursor(Qt.CursorShape.CrossCursor)

    def cancel_target_pick(self):
        """Cancel target-picking mode."""
        self._picking_target = False
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.update()

    def _paint_tile(self, col: int, row: int, tile_num: int):
        """Set a tile at (col, row) to tile_num."""
        if not self.segment:
            return
        if self.segment.data[row][col] != tile_num:
            if not self._paint_undo_pushed:
                self._push_undo()
                self._paint_undo_pushed = True
            self.segment.data[row][col] = tile_num
            self.update()
            self.segment_modified.emit()

    def paintEvent(self, event):
        if not self.segment or not self.tile_cache:
            return

        painter = QPainter(self)
        seg = self.segment
        s = self._tile_px()

        # Draw the outer wall ring (in widget coords), then shift the origin
        # so that everything below is drawn in data tile coords.
        full_w = (seg.width + 2 * RING) * s
        full_h = (seg.height + 2 * RING) * s
        # Hatched in the olive-browns of the base module's wall.bmp so the
        # ring reads as wall.
        painter.fillRect(0, 0, full_w, full_h, RING_BASE_COLOR)
        painter.fillRect(0, 0, full_w, full_h,
                         QBrush(RING_HATCH_COLOR, Qt.BrushStyle.BDiagPattern))
        painter.setPen(QPen(QColor(0, 0, 0), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(RING * s, RING * s, seg.width * s, seg.height * s)
        painter.translate(RING * s, RING * s)

        # Draw tiles - only those touching the area being repainted. For a
        # large segment in a scroll area, this is just the visible part.
        clip = event.rect()
        col0 = max(0, (clip.left() - RING * s) // s)
        col1 = min(seg.width - 1, (clip.right() - RING * s) // s)
        row0 = max(0, (clip.top() - RING * s) // s)
        row1 = min(seg.height - 1, (clip.bottom() - RING * s) // s)
        for row in range(row0, row1 + 1):
            for col in range(col0, col1 + 1):
                tile_num = seg.data[row][col]
                pm = self.tile_cache.get_tile_pixmap(tile_num)
                painter.drawPixmap(col * s, row * s, s, s, pm)

        # Draw rooms
        # Room coordinates use a system where (1,1) = top-left data tile,
        # and (0,0) is the outer wall. So offset by -1 tile.
        # Filled overlays only shown in Rooms mode; borders always shown.
        hover_room_idx = self._hovered_room_index(seg) if self.edit_mode == EditMode.ROOMS else -1

        if self.edit_mode == EditMode.ROOMS:
            room_colors = [
                QColor(255, 165, 0, 50),   # orange
                QColor(100, 180, 255, 50),  # blue
                QColor(100, 220, 100, 50),  # green
                QColor(220, 100, 220, 50),  # purple
                QColor(255, 255, 100, 50),  # yellow
                QColor(255, 100, 100, 50),  # red
                QColor(100, 220, 220, 50),  # cyan
            ]
            hover_colors = [
                QColor(255, 165, 0, 120),
                QColor(100, 180, 255, 120),
                QColor(100, 220, 100, 120),
                QColor(220, 100, 220, 120),
                QColor(255, 255, 100, 120),
                QColor(255, 100, 100, 120),
                QColor(100, 220, 220, 120),
            ]
            painter.setPen(Qt.PenStyle.NoPen)
            for i, room in enumerate(seg.rooms):
                rx = (room.x - 1) * s
                ry = (room.y - 1) * s
                if i == hover_room_idx:
                    painter.setBrush(hover_colors[i % len(hover_colors)])
                else:
                    painter.setBrush(room_colors[i % len(room_colors)])
                painter.drawRect(rx, ry, room.w * s, room.h * s)

        # Draw thin borders on top (only in Rooms mode)
        if self.edit_mode == EditMode.ROOMS:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(255, 165, 0, 160), 1))
            for room in seg.rooms:
                rx = (room.x - 1) * s
                ry = (room.y - 1) * s
                painter.drawRect(rx, ry, room.w * s, room.h * s)

            if self.show_problems:
                self._draw_problems(painter, s)

        # Draw switches
        if self.edit_mode == EditMode.SWITCHES:
            hover_sw = self._hovered_switch_index()
            # Determine which switch to highlight (selected takes priority)
            active_sw = self._selected_switch if self._selected_switch >= 0 else hover_sw

            # Draw markers on all switch tiles
            for i, sw in enumerate(seg.switches):
                if i == active_sw:
                    color = QColor(255, 80, 80, 180)
                else:
                    color = QColor(255, 0, 0, 100)
                painter.fillRect(sw.x * s + 2, sw.y * s + 2, s - 4, s - 4, color)

            # Draw a box around the selected switch for extra contrast
            if 0 <= self._selected_switch < len(seg.switches):
                sw = seg.switches[self._selected_switch]
                painter.setPen(QPen(QColor(255, 255, 0), 2))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(sw.x * s + 1, sw.y * s + 1, s - 2, s - 2)

            # Draw connections for the active (hovered or selected) switch
            if 0 <= active_sw < len(seg.switches):
                highlight = (self.highlighted_func
                             if active_sw == self._selected_switch else -1)
                self._draw_switch_connections(painter, seg.switches[active_sw],
                                              s, highlight)

            # Draw preview line during target picking
            if self._picking_target and self._hover_pos and 0 <= self._selected_switch < len(seg.switches):
                sw = seg.switches[self._selected_switch]
                sx = sw.x * s + s // 2
                sy = sw.y * s + s // 2
                hcol, hrow = self._hover_pos
                tx = hcol * s + s // 2
                ty = hrow * s + s // 2
                painter.setPen(QPen(QColor(255, 255, 255, 150), 2, Qt.PenStyle.DashLine))
                painter.drawLine(sx, sy, tx, ty)
                # Draw target highlight
                painter.setBrush(QColor(255, 255, 255, 60))
                painter.setPen(QPen(QColor(255, 255, 255, 150), 1))
                painter.drawEllipse(tx - s // 3, ty - s // 3, s * 2 // 3, s * 2 // 3)

        # Draw hover highlight
        if self._hover_pos:
            col, row = self._hover_pos
            painter.setPen(QPen(QColor(255, 255, 255, 180), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(col * s, row * s, s, s)

            # Show tile brush preview in paint mode
            left_tile = self.brush_tiles.get(1)
            if (self.edit_mode == EditMode.PAINT and left_tile is not None
                    and self._mouse_pressed is None and not self._on_ring(col, row)):
                pm = self.tile_cache.get_tile_pixmap(left_tile)
                painter.setOpacity(0.5)
                painter.drawPixmap(col * s, row * s, s, s, pm)
                painter.setOpacity(1.0)

        # Draw drag rectangle (rooms mode)
        drag = self._drag_rect()
        if drag and self.edit_mode == EditMode.ROOMS:
            x, y, w, h = drag
            painter.setPen(QPen(QColor(255, 255, 255), 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(x * s, y * s, w * s, h * s)

        if self.edit_mode == EditMode.SELECT:
            # The selection (or the rectangle about to replace it)
            rect = drag or self._selection
            if rect:
                self._draw_selection_rect(painter, s, rect)
            # Paste preview: the clipboard block under the mouse
            block = _clipboard
            if self._pasting and self._hover_pos and block is not None:
                self._draw_paste_preview(painter, s, block, *self._hover_pos)

        painter.end()

    def _draw_selection_rect(self, painter: QPainter, s: int, rect: Rect):
        """Outline a rectangle (data tile coords) in black and white
        dashes, with a light wash over it, so it shows on any tiles."""
        x, y, w, h = rect
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 40))
        painter.drawRect(x * s, y * s, w * s, h * s)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(0, 0, 0), 2))
        painter.drawRect(x * s + 1, y * s + 1, w * s - 2, h * s - 2)
        painter.setPen(QPen(QColor(255, 255, 255), 2, Qt.PenStyle.DashLine))
        painter.drawRect(x * s + 1, y * s + 1, w * s - 2, h * s - 2)

    def _draw_paste_preview(self, painter: QPainter, s: int, block: MapBlock,
                            col: int, row: int):
        """Draw `block` translucently with its top-left tile at (col, row),
        as it would be pasted there: tiles off the data area are not
        shown (they would be dropped), but the outline shows the whole
        block, so a partly-clipped paste is apparent."""
        seg = self.segment
        painter.setOpacity(0.5)
        for r, tile_row in enumerate(block.tiles):
            for c, tile in enumerate(tile_row):
                dc, dr = col + c, row + r
                if tile is not None and 0 <= dc < seg.width and 0 <= dr < seg.height:
                    pm = self.tile_cache.get_tile_pixmap(tile)
                    painter.drawPixmap(dc * s, dr * s, s, s, pm)
        painter.setOpacity(1.0)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(255, 255, 255, 200), 2))
        painter.drawRect(col * s + 1, row * s + 1,
                         block.width * s - 2, block.height * s - 2)

    def _draw_problems(self, painter: QPainter, s: int):
        """Draw the room check markers (painter origin at data tile (0, 0)):
        a hatched fill where rooms overlap, a thick outline around a bad
        room, and a cross on a bad tile. Fills go first so outlines and
        crosses stay legible on top of them."""
        def px_rect(p: RoomProblem) -> tuple[int, int, int, int]:
            x, y, w, h = p.rect  # room coords: data tile (0, 0) is (1, 1)
            return (x - 1) * s, (y - 1) * s, w * s, h * s

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(255, 40, 40, 150), Qt.BrushStyle.BDiagPattern))
        for p in self.problems:
            if p.kind == ProblemKind.OVERLAP:
                painter.drawRect(*px_rect(p))

        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(255, 40, 40, 230), 3))
        for p in self.problems:
            if p.kind == ProblemKind.ROOM:
                x, y, w, h = px_rect(p)
                painter.drawRect(x + 1, y + 1, w - 3, h - 3)

        painter.setPen(QPen(QColor(255, 40, 40, 220), 2))
        for p in self.problems:
            if p.kind == ProblemKind.TILE:
                x, y, _, _ = px_rect(p)
                painter.drawLine(x + 3, y + 3, x + s - 3, y + s - 3)
                painter.drawLine(x + s - 3, y + 3, x + 3, y + s - 3)

    def _enclosing_scroll_area(self) -> QAbstractScrollArea | None:
        w = self.parentWidget()
        while w is not None and not isinstance(w, QAbstractScrollArea):
            w = w.parentWidget()
        return w

    def wheelEvent(self, event):
        if not (event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            super().wheelEvent(event)
            return

        pos = event.position()
        old_px = self._tile_px()
        if event.angleDelta().y() > 0:
            self.zoom_in()
        elif event.angleDelta().y() < 0:
            self.zoom_out()
        new_px = self._tile_px()
        event.accept()
        if new_px == old_px:
            return

        # Keep the map point under the cursor stationary in the viewport
        area = self._enclosing_scroll_area()
        if area:
            ratio = new_px / old_px
            for bar, coord in ((area.horizontalScrollBar(), pos.x()),
                               (area.verticalScrollBar(), pos.y())):
                bar.setValue(round(coord * ratio - (coord - bar.value())))

    def _include_ring(self) -> bool:
        """Whether the wall ring is clickable in the current mode: it is
        where rooms are placed and selections may extend onto it."""
        return self.edit_mode in (EditMode.ROOMS, EditMode.SELECT)

    def mousePressEvent(self, event):
        self._pick_just_finished = False
        pos = self._tile_at(event.position().toPoint(),
                            include_ring=self._include_ring())
        if not pos or not self.segment:
            return

        col, row = pos

        if self.edit_mode == EditMode.SELECT:
            if event.button() == Qt.MouseButton.LeftButton:
                if self._pasting:
                    self._stamp(col, row)
                else:
                    self._drag_start = (col, row)
                    self._drag_end = (col, row)
                    self._mouse_pressed = 1
                    self.update()
                    self._emit_hover_info()
            elif event.button() == Qt.MouseButton.RightButton:
                if self._pasting:
                    self.cancel_paste()
                else:
                    self.clear_selection()

        elif self.edit_mode == EditMode.PAINT:
            button = BRUSH_BUTTONS.get(event.button())
            if button is None:
                return
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                # Pick tile from map
                tile_num = self.segment.data[row][col]
                self.tile_picked.emit(tile_num, button)
            else:
                self._mouse_pressed = button
                self._paint_undo_pushed = False  # new stroke
                tile = self.brush_tiles.get(button)
                if tile is not None:
                    self._paint_tile(col, row, tile)
                # The brush preview is hidden while the mouse is pressed
                self.update()

        elif self.edit_mode == EditMode.ROOMS:
            if event.button() == Qt.MouseButton.LeftButton:
                self._drag_start = (col, row)
                self._drag_end = (col, row)
                self._mouse_pressed = 1
                self._emit_hover_info()
            elif event.button() == Qt.MouseButton.RightButton:
                # Delete room at position
                # Convert data coords (col,row) to room coords (col+1,row+1)
                rc, rr = col + 1, row + 1
                self._push_undo()
                self.segment.rooms = [r for r in self.segment.rooms
                                      if not (r.x <= rc < r.x + r.w and r.y <= rr < r.y + r.h)]
                self.update()
                self.segment_modified.emit()
                self._emit_hover_info()

        elif self.edit_mode == EditMode.SWITCHES:
            if event.button() == Qt.MouseButton.LeftButton:
                if self._picking_target:
                    # Finish target pick
                    self._picking_target = False
                    self._pick_just_finished = True
                    self.setCursor(Qt.CursorShape.ArrowCursor)
                    self.target_picked.emit(col, row)
                else:
                    # Drag a target circle of the selected switch to
                    # retarget that function
                    if 0 <= self._selected_switch < len(self.segment.switches):
                        sel = self.segment.switches[self._selected_switch]
                        fi = self._func_target_at(sel, col, row)
                        if fi >= 0:
                            self._dragging_func = fi
                            self._drag_undo_pushed = False
                            self.setCursor(Qt.CursorShape.ClosedHandCursor)
                            return
                    # Select/deselect switch (double-click creates new)
                    idx = self._switch_at(col, row)
                    if idx >= 0:
                        # Toggle selection
                        if idx == self._selected_switch:
                            self._selected_switch = -1
                        else:
                            self._selected_switch = idx
                    else:
                        # Empty square: just deselect
                        self._selected_switch = -1
                    self.switch_selected.emit(self._selected_switch)
                    self._emit_hover_info()
                self.update()
            elif event.button() == Qt.MouseButton.RightButton:
                if self._picking_target:
                    # Cancel target pick; re-emit the selection so the
                    # editor panel's prompt text resets
                    self._picking_target = False
                    self.setCursor(Qt.CursorShape.ArrowCursor)
                    self.switch_selected.emit(self._selected_switch)
                    self.update()
                else:
                    # If the selected switch has effects targeting this
                    # square, delete those; otherwise delete the switch here
                    if 0 <= self._selected_switch < len(self.segment.switches):
                        sel = self.segment.switches[self._selected_switch]
                        hits = [f for f in sel.funcs
                                if f.has_target
                                and (sel.x + f.dx, sel.y + f.dy) == (col, row)]
                        if hits:
                            self._push_undo()
                            sel.funcs = [f for f in sel.funcs if f not in hits]
                            sel.rebuild_raw_text()
                            self.switch_selected.emit(self._selected_switch)
                            self.update()
                            self.segment_modified.emit()
                            self._emit_hover_info()
                            return
                    self._push_undo()
                    self.segment.switches = [s for s in self.segment.switches
                                             if not (s.x == col and s.y == row)]
                    self._selected_switch = -1
                    self.switch_selected.emit(-1)
                    self.update()
                    self.segment_modified.emit()
                    self._emit_hover_info()

    def mouseDoubleClickEvent(self, event):
        if (self.edit_mode != EditMode.SWITCHES
                or event.button() != Qt.MouseButton.LeftButton
                or self._picking_target):
            super().mouseDoubleClickEvent(event)
            return
        if self._pick_just_finished:
            # This double-click's first press completed a target pick;
            # don't also create a switch at the target square.
            self._pick_just_finished = False
            return
        pos = self._tile_at(event.position().toPoint())
        if not pos or not self.segment:
            return
        col, row = pos
        idx = self._switch_at(col, row)
        if idx >= 0:
            # Double-click on an existing switch just selects it
            self._selected_switch = idx
        else:
            # Create new empty switch
            self._push_undo()
            raw = f"{col} {row} 0"
            sw = Switch(col, row, raw, [])
            self.segment.switches.append(sw)
            self._selected_switch = len(self.segment.switches) - 1
            self.segment_modified.emit()
        self.switch_selected.emit(self._selected_switch)
        self._emit_hover_info()
        self.update()

    def mouseReleaseEvent(self, event):
        if self._dragging_func >= 0:
            self.setCursor(Qt.CursorShape.ArrowCursor)
            if (self._drag_undo_pushed and self.segment
                    and 0 <= self._selected_switch < len(self.segment.switches)):
                sel = self.segment.switches[self._selected_switch]
                sel.rebuild_raw_text()
                self.switch_selected.emit(self._selected_switch)
                self.segment_modified.emit()
            self._dragging_func = -1
            self.update()

        if self.edit_mode == EditMode.ROOMS:
            new_room = self._drag_room()
            if new_room is not None:
                self._push_undo()
                # Remove any existing rooms whose interior the new room
                # intrudes on (or vice versa); sharing an edge line is fine.
                self.segment.rooms = [r for r in self.segment.rooms
                                      if not rooms_overlap(r, new_room)]
                self.segment.rooms.append(new_room)
                self.segment_modified.emit()
            self._drag_start = None
            self._drag_end = None
            self.update()
            self._emit_hover_info()

        elif self.edit_mode == EditMode.SELECT:
            rect = self._drag_rect()
            self._drag_start = None
            self._drag_end = None
            if rect is not None:
                self._set_selection(rect)

        if self._mouse_pressed is not None:
            self._mouse_pressed = None
            self.update()  # brush preview reappears in Paint mode
        self._paint_undo_pushed = False

    def keyPressEvent(self, event):
        if self.edit_mode == EditMode.SELECT:
            if event.key() == Qt.Key.Key_Escape:
                if self._pasting:
                    self.cancel_paste()
                else:
                    self.clear_selection()
                return
        super().keyPressEvent(event)

    def mouseMoveEvent(self, event):
        pos = self._tile_at(event.position().toPoint(),
                            include_ring=self._include_ring())
        prev = self._hover_pos
        self._hover_pos = pos
        # Nothing drawn depends on the mouse position within a tile, so a
        # move that stays on the same tile needs no repaint at all.
        hover_changed = pos != prev
        # Set when an edit or drag altered something on the map.
        dirty = False

        if pos and self.segment:
            col, row = pos
            if self._on_ring(col, row):
                self.tile_hovered.emit(-1)
            else:
                self.tile_hovered.emit(self.segment.data[row][col])

            if self.edit_mode == EditMode.PAINT and self._mouse_pressed:
                tile = self.brush_tiles.get(self._mouse_pressed)
                if tile is not None:
                    self._paint_tile(col, row, tile)  # repaints if changed

            elif (self.edit_mode in (EditMode.ROOMS, EditMode.SELECT)
                    and self._drag_start):
                if self._drag_end != (col, row):
                    self._drag_end = (col, row)
                    dirty = True

            elif (self.edit_mode == EditMode.SWITCHES
                    and self._dragging_func >= 0
                    and 0 <= self._selected_switch < len(self.segment.switches)):
                sel = self.segment.switches[self._selected_switch]
                if self._dragging_func < len(sel.funcs):
                    f = sel.funcs[self._dragging_func]
                    dx, dy = col - sel.x, row - sel.y
                    if (dx, dy) != (f.dx, f.dy):
                        if not self._drag_undo_pushed:
                            self._push_undo()
                            self._drag_undo_pushed = True
                        while len(f.args) < 2:
                            f.args.append(0)
                        f.args[0], f.args[1] = dx, dy
                        dirty = True
        else:
            self.tile_hovered.emit(-1)

        if hover_changed:
            self._emit_hover_info()
        if dirty or hover_changed:
            # Repaint the whole widget; Qt clips this to the visible part
            # of the scroll area, and paintEvent only draws tiles within
            # that, so this stays cheap even for large segments.
            self.update()

    def leaveEvent(self, event):
        self._hover_pos = None
        self.tile_hovered.emit(-1)
        self._emit_hover_info()
        self.update()
