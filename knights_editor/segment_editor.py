"""Segment editor widget — combines map canvas, tile palette, segment list, and properties."""

import copy
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter, QScrollArea,
    QRadioButton, QButtonGroup, QLabel, QLineEdit, QGroupBox,
    QFormLayout, QListWidget, QPushButton, QGridLayout, QMessageBox,
    QApplication, QSizePolicy, QInputDialog, QCheckBox, QMenu, QSpinBox,
)
from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QShortcut, QKeySequence

from knights_editor.models import Segment, TileEntry, Switch, SwitchFunc, MIN_SEGMENT_SIZE
from knights_editor.map_widget import MapWidget, EditMode, clipboard_block
from knights_editor.auto_rooms import auto_rooms
from knights_editor.room_check import check_rooms, RoomProblem
from knights_editor.config import get_show_room_problems, set_show_room_problems
from knights_editor.tile_palette_widget import TilePaletteWidget, BRUSH_LABELS
from knights_editor.segment_list_widget import SegmentListWidget
from knights_editor.segment_io import load_segments_with_line_ending, save_segments
from knights_editor.line_endings import PLATFORM_DEFAULT
from knights_editor.file_stamp import file_stamp
from knights_editor.graphics_cache import GraphicsCache, TilePixmapCache


def default_fill_tile(tile_table: dict[int, TileEntry]) -> int | None:
    """Tile number to fill new segments with.

    Picks the lowest-numbered entry that is a single tile whose Lua
    name contains "floor" (e.g. t_floor1); if none, the lowest-numbered
    entry. None if the tile table is empty.
    """
    if not tile_table:
        return None
    floors = [num for num, entry in tile_table.items()
              if len(entry.tiles) == 1 and entry.tiles[0].name
              and "floor" in entry.tiles[0].name.lower()]
    if floors:
        return min(floors)
    return min(tile_table)


# Largest segment size the properties panel accepts. Generous: the map
# widget is (size + 2) * 32 px at 100% zoom, so this stays well inside
# Qt's widget size limit even zoomed in.
MAX_SEGMENT_SIZE = 999


class SegmentEditor(QWidget):
    """Editor for a single .txt segment file. Contains segment list, map, palette, properties."""

    modified_changed = Signal(bool)  # emitted when unsaved state changes

    def __init__(self, filepath: str, tile_table: dict[int, TileEntry],
                 gfx_cache: GraphicsCache, parent=None):
        super().__init__(parent)
        self.filepath = filepath
        self.tile_table = tile_table
        self.gfx_cache = gfx_cache
        # Composited tile pixmaps for this file's tile table; replaced
        # along with the table in set_tile_table.
        self.tile_cache = TilePixmapCache(tile_table, gfx_cache)
        self.segments: list[Segment] = []
        self.line_ending = PLATFORM_DEFAULT  # detected on load, preserved on save
        self._unsaved = False

        # State for switch function being added
        self._pending_func_name: str | None = None
        self._pending_shoot_dir: int | None = None

        # Guards against editingFinished re-firing while the duplicate-name
        # warning dialog has taken focus away from the name field
        self._name_warning_open = False

        self._setup_ui()
        self._load()

    @property
    def unsaved(self) -> bool:
        return self._unsaved

    @unsaved.setter
    def unsaved(self, value: bool):
        if self._unsaved != value:
            self._unsaved = value
            self.modified_changed.emit(value)

    def _setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left: segment list
        self.seg_list = SegmentListWidget()
        self.seg_list.setMaximumWidth(200)
        self.seg_list.segment_selected.connect(self._on_segment_selected)
        self.seg_list.segment_moved.connect(self._on_segment_moved)
        self.seg_list.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.seg_list.customContextMenuRequested.connect(
            self._on_list_context_menu)
        splitter.addWidget(self.seg_list)

        # Center: map + mode bar + palette
        center = QWidget()
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(0, 0, 0, 0)

        self.map_widget = MapWidget()
        self.map_widget.set_tile_table(self.tile_table, self.tile_cache)
        self.map_widget.tile_picked.connect(self._on_tile_picked)
        self.map_widget.tile_hovered.connect(self._on_map_tile_hovered)
        self.map_widget.room_hovered.connect(self._on_room_hovered)
        self.map_widget.problem_hovered.connect(self._on_problem_hovered)
        self.map_widget.switch_hovered.connect(self._on_switch_hovered)
        self.map_widget.switch_selected.connect(self._on_switch_selected)
        self.map_widget.segment_modified.connect(self._on_segment_modified)
        self.map_widget.target_picked.connect(self._on_target_picked)
        self.map_widget.selection_changed.connect(self._on_selection_changed)
        self.map_widget.pasting_changed.connect(self._on_pasting_changed)
        # Delete clears the selected area (when the map has the focus)
        QShortcut(QKeySequence(Qt.Key.Key_Delete), self.map_widget,
                  self.clear_selection,
                  context=Qt.ShortcutContext.WidgetShortcut)

        map_scroll = QScrollArea()
        map_scroll.setWidget(self.map_widget)
        map_scroll.setWidgetResizable(False)
        center_layout.addWidget(map_scroll, stretch=3)

        # Mode buttons
        mode_bar = QHBoxLayout()
        self.mode_group = QButtonGroup(self)
        for mode, label in [(EditMode.SELECT, "Select"), (EditMode.PAINT, "Paint"),
                            (EditMode.SWITCHES, "Switches"), (EditMode.ROOMS, "Rooms")]:
            btn = QRadioButton(label)
            if mode == self.map_widget.edit_mode:
                btn.setChecked(True)
            self.mode_group.addButton(btn, mode.value)
            mode_bar.addWidget(btn)

        # One "L: 12"-style label per brush button. Left and right are
        # always shown; the others appear once a tile is chosen with them.
        mode_bar.addStretch()
        self.brush_tile_labels: dict[int, QLabel] = {}
        for button, name in sorted(BRUSH_LABELS.items()):
            label = QLabel(f"{name}: -")
            label.setVisible(button <= 2)
            mode_bar.addWidget(label)
            self.brush_tile_labels[button] = label
        mode_bar.addSpacing(12)
        self.map_zoom_label = self._add_zoom_controls(mode_bar, "Zoom:", self.map_widget)
        center_layout.addLayout(mode_bar)
        self.mode_group.idToggled.connect(self._on_mode_changed)

        # Tile palette
        self.tile_palette = TilePaletteWidget()
        self.tile_palette.set_tile_table(self.tile_table, self.tile_cache)
        self.tile_palette.tile_selected.connect(self._on_tile_selected)
        self.tile_palette.tile_hovered.connect(self._on_palette_tile_hovered)
        # Brush outlines are Paint-only (see _on_mode_changed)
        self.tile_palette.show_selection = (
            self.map_widget.edit_mode == EditMode.PAINT)

        palette_scroll = QScrollArea()
        palette_scroll.setWidget(self.tile_palette)
        palette_scroll.setWidgetResizable(True)
        palette_scroll.setMaximumHeight(200)
        center_layout.addWidget(palette_scroll, stretch=1)

        status_bar = QHBoxLayout()
        # Describes what is under the mouse, according to the edit mode:
        # the tile (Paint), the room being dragged out or hovered (Rooms),
        # or the switch (Switches).
        self.hover_status = QLabel("")
        # Don't let long hover text impose a minimum width on the layout,
        # which would make the splitter panels jump about in narrow windows
        # (long text is simply clipped instead).
        self.hover_status.setSizePolicy(QSizePolicy.Policy.Ignored,
                                        QSizePolicy.Policy.Preferred)
        status_bar.addWidget(self.hover_status, stretch=1)
        self.palette_zoom_label = self._add_zoom_controls(
            status_bar, "Palette zoom:", self.tile_palette)
        center_layout.addLayout(status_bar)

        splitter.addWidget(center)

        # Right: properties
        right = QWidget()
        right.setMaximumWidth(250)
        right_layout = QVBoxLayout(right)

        props = QGroupBox("Segment Properties")
        form = QFormLayout(props)
        self.name_edit = QLineEdit()
        self.name_edit.editingFinished.connect(self._on_name_changed)
        form.addRow("Name:", self.name_edit)
        # Size: applied (undoably) on Enter or focus loss, like the name -
        # not per keystroke, since typing "20" would first shrink the
        # segment to 2 and throw away the tiles beyond.
        self.width_spin = QSpinBox()
        self.height_spin = QSpinBox()
        for spin in (self.width_spin, self.height_spin):
            spin.setRange(MIN_SEGMENT_SIZE, MAX_SEGMENT_SIZE)
            spin.editingFinished.connect(self._on_size_changed)
        form.addRow("Width:", self.width_spin)
        form.addRow("Height:", self.height_spin)
        right_layout.addWidget(props)

        info = QGroupBox("Info")
        info_layout = QVBoxLayout(info)
        self.info_label = QLabel("No segment loaded")
        info_layout.addWidget(self.info_label)
        right_layout.addWidget(info)

        # Mode-specific panels: one or more per edit mode (none for Paint).
        # Only the current mode's panels are shown; the others are hidden,
        # which also removes them from the layout (see _show_mode_page).
        self._mode_pages: dict[EditMode, list[QWidget]] = {}

        # Select panel
        select_box = QGroupBox("Select")
        self._mode_pages.setdefault(EditMode.SELECT, []).append(select_box)
        right_layout.addWidget(select_box)
        select_layout = QVBoxLayout(select_box)
        select_help = QLabel(
            "Drag to select a rectangular area, then use the Cut, Copy, "
            "Paste or Clear buttons below.")
        select_help.setWordWrap(True)
        select_layout.addWidget(select_help)
        select_grid = QGridLayout()
        self.btn_cut = QPushButton("Cut")
        self.btn_copy = QPushButton("Copy")
        self.btn_paste = QPushButton("Paste")
        self.btn_clear_sel = QPushButton("Clear")
        self.btn_clear_sel.setToolTip(
            "Replace the selected area with plain floor, deleting its "
            "switches and rooms (Delete key).")
        select_grid.addWidget(self.btn_cut, 0, 0)
        select_grid.addWidget(self.btn_copy, 0, 1)
        select_grid.addWidget(self.btn_paste, 1, 0)
        select_grid.addWidget(self.btn_clear_sel, 1, 1)
        select_layout.addLayout(select_grid)
        self.btn_cut.clicked.connect(self.cut)
        self.btn_copy.clicked.connect(self.copy)
        self.btn_paste.clicked.connect(self.paste)
        self.btn_clear_sel.clicked.connect(self.clear_selection)
        # Pieces of the Select-mode hover status line (see _update_select_status)
        self._selection_text = ""
        self._hovered_tile_text = ""
        self._update_selection_buttons()

        # Room check (Rooms mode): refreshed on every change. Shows only a
        # problem count; the problems themselves are marked on the map
        # (when "Show on map" is ticked, which can be done at any time,
        # including when there are none yet) and described in the hover
        # status line when the mouse is over them.
        check_box = QGroupBox("Room Check")
        self._mode_pages.setdefault(EditMode.ROOMS, []).append(check_box)
        # Never take more than its size hint (spare space goes to the
        # stretch at the bottom of the column).
        check_box.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        check_layout = QVBoxLayout(check_box)
        self.check_label = QLabel("")
        check_layout.addWidget(self.check_label)
        self.show_problems_check = QCheckBox("Show on map")
        self.show_problems_check.setToolTip(
            "Mark the problems on the map in red. Point at a marker to see "
            "what the problem is.")
        self.show_problems_check.setChecked(get_show_room_problems())
        self.show_problems_check.toggled.connect(self._on_show_problems_toggled)
        check_layout.addWidget(self.show_problems_check)
        right_layout.addWidget(check_box)
        self._check_problems: list[RoomProblem] = []
        # Pieces of the Rooms-mode hover status line (see _update_rooms_status)
        self._hovered_room_text = ""
        self._hovered_problem_text = ""

        # Switch panel
        switch_box = QGroupBox("Switch")
        self._mode_pages.setdefault(EditMode.SWITCHES, []).append(switch_box)
        right_layout.addWidget(switch_box)
        switch_layout = QVBoxLayout(switch_box)
        switch_help = QLabel(
            "Click on a switch to select. Double-click to create a new "
            "switch. Right-click to delete."
        )
        switch_help.setWordWrap(True)
        switch_layout.addWidget(switch_help)
        self.switch_label = QLabel("No switch selected currently.")
        self.switch_label.setWordWrap(True)
        switch_layout.addWidget(self.switch_label)

        self.switch_func_list = QListWidget()
        self.switch_func_list.setMaximumHeight(120)
        switch_layout.addWidget(self.switch_func_list)

        self.btn_remove_func = QPushButton("Remove")
        switch_layout.addWidget(self.btn_remove_func)

        add_box = QGroupBox("Add to selected switch:")
        add_layout = QVBoxLayout(add_box)
        self.btn_add_toggle = QPushButton("Toggle")
        self.btn_add_open = QPushButton("Open")
        self.btn_add_close = QPushButton("Close")
        self.btn_add_shoot_n = QPushButton("Shoot N")
        self.btn_add_shoot_e = QPushButton("Shoot E")
        self.btn_add_shoot_s = QPushButton("Shoot S")
        self.btn_add_shoot_w = QPushButton("Shoot W")

        add_row = QHBoxLayout()
        add_row.addWidget(self.btn_add_toggle)
        add_row.addWidget(self.btn_add_open)
        add_row.addWidget(self.btn_add_close)
        add_layout.addLayout(add_row)

        self.btn_add_custom = QPushButton("Custom")
        shoot_grid = QGridLayout()
        shoot_grid.addWidget(self.btn_add_shoot_n, 0, 1)
        shoot_grid.addWidget(self.btn_add_shoot_w, 1, 0)
        shoot_grid.addWidget(self.btn_add_shoot_s, 2, 1)
        shoot_grid.addWidget(self.btn_add_shoot_e, 1, 2)
        shoot_grid.addWidget(self.btn_add_custom, 1, 1)
        add_layout.addLayout(shoot_grid)

        # Prompt shown while picking a target tile; kept below the buttons
        # so appearing/disappearing text doesn't shift them.
        self.target_prompt = QLabel("")
        self.target_prompt.setWordWrap(True)
        add_layout.addWidget(self.target_prompt)
        switch_layout.addWidget(add_box)

        # Rooms panel
        rooms_box = QGroupBox("Rooms")
        self._mode_pages.setdefault(EditMode.ROOMS, []).append(rooms_box)
        right_layout.addWidget(rooms_box)
        rooms_layout = QVBoxLayout(rooms_box)
        rooms_help = QLabel(
            "Drag to create a room, including its outer walls.\n"
            "Right-click to delete a room."
        )
        rooms_help.setWordWrap(True)
        rooms_layout.addWidget(rooms_help)
        self.btn_auto_rooms = QPushButton("Auto Rooms")
        self.btn_auto_rooms.setToolTip(
            "Place rooms automatically based on the walls and doors, "
            "filling in only the areas not already covered by a room.\n"
            "(To start from scratch, use Delete All Rooms first.)\n"
            "Auto Rooms doesn't always work perfectly, so you may need "
            "to adjust the results by hand afterwards.")
        self.btn_auto_rooms.clicked.connect(self._auto_rooms)
        rooms_layout.addWidget(self.btn_auto_rooms)
        self.btn_delete_all_rooms = QPushButton("Delete All Rooms")
        self.btn_delete_all_rooms.clicked.connect(self._delete_all_rooms)
        rooms_layout.addWidget(self.btn_delete_all_rooms)

        self._show_mode_page(self.map_widget.edit_mode)

        self._set_switch_buttons_enabled(False)

        self.btn_add_toggle.clicked.connect(lambda: self._start_add_func("toggle"))
        self.btn_add_open.clicked.connect(lambda: self._start_add_func("open"))
        self.btn_add_close.clicked.connect(lambda: self._start_add_func("close"))
        self.btn_add_shoot_n.clicked.connect(lambda: self._start_add_func("shoot", 1))
        self.btn_add_shoot_e.clicked.connect(lambda: self._start_add_func("shoot", 2))
        self.btn_add_shoot_s.clicked.connect(lambda: self._start_add_func("shoot", 3))
        self.btn_add_shoot_w.clicked.connect(lambda: self._start_add_func("shoot", 4))
        self.btn_add_custom.clicked.connect(self._add_custom_func)
        self.btn_remove_func.clicked.connect(self._remove_selected_func)
        self.switch_func_list.currentRowChanged.connect(self._on_func_row_changed)
        self.switch_func_list.itemDoubleClicked.connect(self._edit_func)

        right_layout.addStretch()
        splitter.addWidget(right)

        splitter.setSizes([150, 500, 200])
        layout.addWidget(splitter)

    def _add_zoom_controls(self, layout: QHBoxLayout, caption: str, widget) -> QLabel:
        """Add "caption  − 100% +" controls driving `widget`'s zoom; return the % label."""
        layout.addWidget(QLabel(caption))
        btn_out = QPushButton("−")
        btn_out.setFixedWidth(24)
        btn_out.clicked.connect(widget.zoom_out)
        layout.addWidget(btn_out)
        zoom_label = QLabel("100%")
        zoom_label.setMinimumWidth(40)
        zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(zoom_label)
        btn_in = QPushButton("+")
        btn_in.setFixedWidth(24)
        btn_in.clicked.connect(widget.zoom_in)
        layout.addWidget(btn_in)
        widget.zoom_changed.connect(lambda z: zoom_label.setText(f"{round(z * 100)}%"))
        return zoom_label

    def zoom_in(self):
        self.map_widget.zoom_in()

    def zoom_out(self):
        self.map_widget.zoom_out()

    def zoom_reset(self):
        self.map_widget.zoom_reset()
        self.tile_palette.zoom_reset()

    def _load(self):
        self.segments, self.line_ending = load_segments_with_line_ending(self.filepath)
        # On-disk state the editor's content corresponds to (see
        # disk_changed); updated after every read and write.
        self.disk_stamp = file_stamp(self.filepath)
        self.seg_list.set_segments([s.name for s in self.segments])
        if not self.segments:
            # The list only reports real selections, so tell the map
            # explicitly that there is nothing to edit (otherwise, on a
            # revert to a file that lost all its segments, it would keep
            # showing - and editing - the discarded one).
            self._on_segment_selected(-1)
        self.unsaved = False

    def disk_changed(self) -> bool:
        """True if the file on disk has been modified (by something other
        than this editor) since it was last read or written here. A file
        that has been deleted doesn't count: there is nothing to reload."""
        stamp = file_stamp(self.filepath)
        return stamp is not None and stamp != self.disk_stamp

    def mark_disk_current(self):
        """Accept the current on-disk state as known, so disk_changed()
        stops reporting it (used when the user chooses to keep the
        editor's version over an external modification)."""
        self.disk_stamp = file_stamp(self.filepath)

    def mark_modified(self):
        """Flag the editor's content as differing from the file on disk
        even though nothing was edited (the disk copy changed under us
        and the user chose to keep this version)."""
        self.unsaved = True

    def set_tile_table(self, tile_table: dict[int, TileEntry],
                       gfx_cache: GraphicsCache):
        """Swap in a new tile table (e.g. after a mod reload).

        Only rendering changes; segment data, selection and undo
        history are untouched.
        """
        self.tile_table = tile_table
        self.gfx_cache = gfx_cache
        self.tile_cache = TilePixmapCache(tile_table, gfx_cache)
        self.map_widget.set_tile_table(tile_table, self.tile_cache)
        self.tile_palette.set_tile_table(tile_table, self.tile_cache)
        self._update_room_check()

    def save(self):
        save_segments(self.filepath, self.segments, self.line_ending)
        self.disk_stamp = file_stamp(self.filepath)
        self.unsaved = False

    def revert(self):
        """Discard unsaved changes by re-reading the file from disk (also
        used to pick up external modifications).

        Selection and undo history are reset, since they refer to the
        discarded data.
        """
        self._load()

    def undo(self):
        # The map widget cancels any in-progress target pick and re-emits
        # switch_selected, which rebuilds the switch panel
        self._pending_func_name = None
        self.map_widget.undo()

    def redo(self):
        self._pending_func_name = None
        self.map_widget.redo()

    # --- Cut/copy/paste of map areas (Select mode) ---

    def copy(self):
        """Copy the selected area of the map to the clipboard."""
        self.map_widget.copy_selection()

    def cut(self):
        """Copy the selected area of the map to the clipboard, then clear
        it (see clear_selection)."""
        fill = self.resize_fill_tile()
        if fill is not None:
            self.map_widget.cut_selection(fill)

    def clear_selection(self):
        """Replace the selected area's tiles with the default floor tile
        and delete its switches and rooms."""
        fill = self.resize_fill_tile()
        if fill is not None:
            self.map_widget.clear_selection_contents(fill)

    def paste(self):
        """Start pasting the clipboard block: switch to Select mode, if
        not there already, and let the user click where it should go."""
        if clipboard_block() is None or not self.map_widget.segment:
            return
        self.mode_group.button(EditMode.SELECT.value).setChecked(True)
        self.map_widget.start_paste()
        self.map_widget.setFocus()  # so that Escape cancels

    def default_fill_tile(self) -> int | None:
        """Tile number to fill new segments with (see module-level
        default_fill_tile)."""
        return default_fill_tile(self.tile_table)

    def select_segment(self, name: str, index: int | None = None) -> bool:
        """Select a segment by name (e.g. when navigating here from the
        Lua state browser). `index` - the segment's position in the file
        when it was loaded - is preferred if it still matches the name,
        and used as a fallback for unnamed/renamed segments. Returns
        False if no matching segment was found."""
        row = None
        if index is not None and 0 <= index < len(self.segments) \
                and self.segments[index].name == name:
            row = index
        else:
            row = next((i for i, s in enumerate(self.segments)
                        if s.name == name), None)
            if row is None and index is not None \
                    and 0 <= index < len(self.segments):
                row = index
        if row is None:
            return False
        self.seg_list.setCurrentRow(row)
        return True

    def current_index(self) -> int:
        """Index (in file order) of the selected segment, or -1 if none."""
        row = self.seg_list.currentRow()
        return row if 0 <= row < len(self.segments) else -1

    def _insert_segment(self, seg: Segment, index: int | None):
        """Insert `seg` at `index` (appending if None), select it and mark
        the file modified."""
        if index is None or not (0 <= index <= len(self.segments)):
            index = len(self.segments)
        self.segments.insert(index, seg)
        self.seg_list.set_segments([s.name for s in self.segments])
        self.seg_list.setCurrentRow(index)
        self.unsaved = True

    def add_segment(self, index: int | None = None):
        """Add a new blank segment at position `index` in the file
        (at the end if None) and select it."""
        default_tile = self.default_fill_tile()
        if default_tile is None:
            QMessageBox.warning(
                self, "No tile table",
                "Cannot create a segment: no tile table is loaded "
                "for this file.")
            return
        existing = {s.name for s in self.segments}
        n = len(self.segments) + 1
        while f"new_{n}" in existing:
            n += 1
        seg = Segment(
            name=f"new_{n}",
            width=12, height=12,
            data=[[default_tile] * 12 for _ in range(12)],
        )
        self._insert_segment(seg, index)

    def duplicate_segment(self, row: int | None = None):
        """Insert an exact copy of the segment at `row` (the selected one
        if None) directly below it, under a fresh unique name, and select
        the copy."""
        if row is None:
            row = self.seg_list.currentRow()
        if not (0 <= row < len(self.segments)):
            return
        src = self.segments[row]
        existing = {s.name for s in self.segments}
        name = f"{src.name}_copy"
        n = 2
        while name in existing:
            name = f"{src.name}_copy{n}"
            n += 1
        seg = copy.deepcopy(src)
        seg.name = name
        seg.source_file = None
        seg.source_index = None
        self._insert_segment(seg, row + 1)

    def delete_segment(self, row: int | None = None):
        """Delete the segment at `row` (the selected one if None), after
        confirmation."""
        if row is None:
            row = self.seg_list.currentRow()
        if not (0 <= row < len(self.segments)):
            return
        reply = QMessageBox.question(self, "Delete Segment",
                                      f"Delete segment '{self.segments[row].name}'?")
        if reply == QMessageBox.StandardButton.Yes:
            self.segments.pop(row)
            self.seg_list.set_segments([s.name for s in self.segments])
            if self.segments:
                # Keep the selection where it was (or on the new last row)
                self.seg_list.setCurrentRow(min(row, len(self.segments) - 1))
            else:
                self._on_segment_selected(-1)
            self.unsaved = True

    def resize_fill_tile(self) -> int | None:
        """Tile number to fill newly exposed tiles with when a segment is
        enlarged: the same one new segments get, falling back to the
        segment's most common tile if there is no suitable table entry.
        None only if there is no segment at all."""
        tile = self.default_fill_tile()
        if tile is not None:
            return tile
        seg = self.map_widget.segment
        if not seg:
            return None
        counts: dict[int, int] = {}
        for row in seg.data:
            for v in row:
                counts[v] = counts.get(v, 0) + 1
        return max(counts, key=lambda v: (counts[v], -v)) if counts else None

    def resize_segment(self, width: int, height: int):
        """Resize the selected segment as an undoable edit (see
        Segment.resized), keeping its top-left corner fixed. Newly
        exposed tiles are filled with resize_fill_tile(). Tiles beyond
        the new size are discarded, along with the switches on them and
        any room no longer fitting within the new bounds."""
        seg = self.map_widget.segment
        if not seg or (width, height) == (seg.width, seg.height):
            return
        fill = self.resize_fill_tile()
        if fill is None:
            return
        self.map_widget.resize_segment(seg.resized(width, height, fill))

    def _on_size_changed(self):
        """Width/Height spin box committed (Enter or focus loss)."""
        if self.map_widget.segment:
            self.resize_segment(self.width_spin.value(), self.height_spin.value())

    def _show_size(self, seg: Segment | None):
        """Show `seg`'s size in the Width/Height spin boxes (disabled if
        there is no segment)."""
        for spin, value in ((self.width_spin, seg.width if seg else MIN_SEGMENT_SIZE),
                            (self.height_spin, seg.height if seg else MIN_SEGMENT_SIZE)):
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)
            spin.setEnabled(seg is not None)

    def _on_list_context_menu(self, pos):
        """Right-click menu on the segment list: Add / Delete / Duplicate,
        acting on the row under the pointer (which becomes selected). On
        blank space only Add is available and appends to the file."""
        index = self.seg_list.indexAt(pos)
        row = index.row() if index.isValid() else -1
        if row >= 0:
            self.seg_list.setCurrentRow(row)

        menu = QMenu(self)
        add_action = menu.addAction("Add")
        delete_action = menu.addAction("Delete")
        duplicate_action = menu.addAction("Duplicate")
        delete_action.setEnabled(row >= 0)
        duplicate_action.setEnabled(row >= 0)
        chosen = menu.exec(self.seg_list.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        # Let the menu's mouse-release handling fully unwind before any
        # of these open a modal dialog (see main_window.deferred).
        if chosen == add_action:
            QTimer.singleShot(
                0, lambda: self.add_segment(row + 1 if row >= 0 else None))
        elif chosen == delete_action:
            QTimer.singleShot(0, lambda: self.delete_segment(row))
        elif chosen == duplicate_action:
            QTimer.singleShot(0, lambda: self.duplicate_segment(row))

    def _on_segment_moved(self, src: int, dst: int):
        """The user dragged a list row from `src` to `dst`: reorder the
        segments to match. The selected segment is unchanged, so the map
        view and its undo history are left alone."""
        if not (0 <= src < len(self.segments)) \
                or not (0 <= dst < len(self.segments)):
            return
        seg = self.segments.pop(src)
        self.segments.insert(dst, seg)
        self.unsaved = True

    # --- Event handlers ---

    def _on_segment_selected(self, index: int):
        if 0 <= index < len(self.segments):
            seg = self.segments[index]
            self.map_widget.set_segment(seg)
            self.name_edit.setText(seg.name)
            self._update_info()
            self._update_room_check()
        elif index < 0:
            # No segment (e.g. the last one was deleted)
            self.map_widget.set_segment(None)
            self.name_edit.setText("")
            self._update_info()
            self._update_room_check()

    def _on_tile_selected(self, tile_num: int, button: int):
        self.map_widget.brush_tiles[button] = tile_num
        self.tile_palette.brush_tiles[button] = tile_num
        label = self.brush_tile_labels[button]
        label.setText(f"{BRUSH_LABELS[button]}: {tile_num}")
        label.setVisible(True)
        self.tile_palette.update()

    def _on_map_tile_hovered(self, tile_num: int):
        mode = self.map_widget.edit_mode
        if mode == EditMode.PAINT:
            self._show_tile_info(tile_num)
        elif mode == EditMode.SELECT:
            self._hovered_tile_text = self._tile_info_text(tile_num)
            self._update_select_status()
        # The status line shows rooms/switches in the other modes

    def _on_palette_tile_hovered(self, tile_num: int):
        # The palette describes its tiles in every mode (only the
        # selection outlines are Paint-only).
        self._show_tile_info(tile_num)

    def _show_tile_info(self, tile_num: int):
        self.hover_status.setText(self._tile_info_text(tile_num))

    def _tile_info_text(self, tile_num: int) -> str:
        """Describe a tile for the status line, e.g. "Tile 12  (t_floor1)";
        "" for a negative (no tile) number."""
        if tile_num < 0:
            return ""
        entry = self.tile_table.get(tile_num)
        info = f"Tile {tile_num}"
        if entry and entry.tiles:
            names = []
            for t in entry.tiles:
                if t.name:
                    names.append(t.name)
                elif t.graphic:
                    # Fall back to the graphic's filename
                    name = t.graphic.filename
                    if '/' in name:
                        name = name.rsplit('/', 1)[1]
                    if name.endswith('.bmp'):
                        name = name[:-4]
                    names.append(name)
            if names:
                info += f"  ({', '.join(names)})"
        return info

    def _on_selection_changed(self, x: int, y: int, w: int, h: int):
        self._selection_text = f"Selection: ({x}, {y})  {w} x {h}" if w > 0 else ""
        self._update_selection_buttons()
        self._update_select_status()

    def _on_pasting_changed(self, pasting: bool):
        self._update_select_status()

    def _update_selection_buttons(self):
        has_sel = self.map_widget.selection is not None
        for btn in (self.btn_cut, self.btn_copy, self.btn_clear_sel):
            btn.setEnabled(has_sel)
        # Paste stays enabled: the clipboard is shared between files, so
        # it can be filled by another tab without this one hearing of it

    def _update_select_status(self):
        """Select-mode status line: what a paste is waiting for, or the
        selection (or the rectangle being dragged out) and the hovered
        tile, e.g. "Selection: (2, 3)  4 x 5  —  Tile 12  (t_floor1)"."""
        if self.map_widget.pasting:
            block = clipboard_block()
            size = f"{block.width} x {block.height}" if block else ""
            text = (f"Click to paste ({size}) with its top-left corner at "
                    "the pointer; right-click or Escape to cancel.")
        else:
            parts = [t for t in (self._selection_text, self._hovered_tile_text) if t]
            text = "  —  ".join(parts)
        self.hover_status.setText(text)

    def _on_room_hovered(self, x: int, y: int, w: int, h: int):
        self._hovered_room_text = f"Room: ({x}, {y})  {w} x {h}" if w > 0 else ""
        self._update_rooms_status()

    def _on_problem_hovered(self, text: str):
        self._hovered_problem_text = text
        self._update_rooms_status()

    def _update_rooms_status(self):
        """Rooms-mode status line: the hovered/dragged room, then any room
        check problems under the mouse, e.g.
        "Room: (2, 3)  4 x 5  --  Tile (3, 4) is not in any room"."""
        parts = [t for t in (self._hovered_room_text, self._hovered_problem_text) if t]
        self.hover_status.setText("  —  ".join(parts))

    def _on_switch_hovered(self, idx: int):
        seg = self.map_widget.segment
        if seg is None or not (0 <= idx < len(seg.switches)):
            self.hover_status.setText("")
            return
        sw = seg.switches[idx]
        funcs = ", ".join(f.description() for f in sw.funcs) or "(no functions)"
        self.hover_status.setText(f"Switch: ({sw.x}, {sw.y})  {funcs}")

    def _on_tile_picked(self, tile_num: int, button: int):
        self._on_tile_selected(tile_num, button)

    def _on_switch_selected(self, index: int):
        self.switch_func_list.clear()
        self.target_prompt.setText("")
        if index < 0 or not self.segments:
            self.switch_label.setText("No switch selected currently.")
            self._set_switch_buttons_enabled(False)
            return
        seg = self.map_widget.segment
        if not seg or index >= len(seg.switches):
            return
        sw = seg.switches[index]
        self.switch_label.setText(f"Switch at ({sw.x}, {sw.y})")
        for func in sw.funcs:
            if func.has_target:
                target_x = sw.x + func.dx
                target_y = sw.y + func.dy
                self.switch_func_list.addItem(
                    f"{func.description()}  -> ({target_x}, {target_y})")
            else:
                self.switch_func_list.addItem(func.description())
        self._set_switch_buttons_enabled(True)

    def _on_func_row_changed(self, row: int):
        # Highlight the corresponding switch function on the map (in white)
        self.map_widget.highlighted_func = row
        self.map_widget.update()
        self.btn_remove_func.setEnabled(row >= 0)

    def _set_switch_buttons_enabled(self, enabled: bool):
        for btn in [self.btn_add_toggle, self.btn_add_open, self.btn_add_close,
                    self.btn_add_shoot_n, self.btn_add_shoot_e,
                    self.btn_add_shoot_s, self.btn_add_shoot_w,
                    self.btn_add_custom]:
            btn.setEnabled(enabled)
        # Remove additionally needs a function to be selected in the list
        self.btn_remove_func.setEnabled(
            enabled and self.switch_func_list.currentRow() >= 0)

    def _start_add_func(self, func_name: str, shoot_dir: int | None = None):
        seg = self.map_widget.segment
        idx = self.map_widget._selected_switch
        if not seg or idx < 0 or idx >= len(seg.switches):
            return
        self._pending_func_name = func_name
        self._pending_shoot_dir = shoot_dir
        self.target_prompt.setText(
            f"Click target tile for '{func_name}' "
            f"(right-click to cancel)."
        )
        self.map_widget.start_target_pick()

    def _on_target_picked(self, col: int, row: int):
        seg = self.map_widget.segment
        idx = self.map_widget._selected_switch
        if not seg or idx < 0 or idx >= len(seg.switches):
            self._pending_func_name = None
            return
        if not self._pending_func_name:
            return
        sw = seg.switches[idx]
        dx = col - sw.x
        dy = row - sw.y
        self.map_widget._push_undo()
        if self._pending_func_name == "shoot":
            args = [dx, dy, self._pending_shoot_dir]
        else:
            args = [dx, dy]
        sw.funcs.append(SwitchFunc(self._pending_func_name, args))
        sw.rebuild_raw_text()
        self._pending_func_name = None
        self._pending_shoot_dir = None
        self._on_switch_selected(idx)
        self.map_widget.update()
        self.map_widget.segment_modified.emit()

    def _prompt_switch_func(self, title: str, initial: str) -> SwitchFunc | None:
        """Ask for a switch function in "name(arg,arg,...)" notation.

        Re-prompts (keeping the entered text) on invalid input; returns
        None if cancelled."""
        text = initial
        while True:
            text, ok = QInputDialog.getText(
                self, title, "Switch function, e.g. toggle(1,-2):",
                text=text)
            if not ok:
                return None
            try:
                return SwitchFunc.parse_text(text)
            except ValueError as e:
                QMessageBox.warning(self, "Invalid switch function", str(e))

    def _current_switch(self) -> Switch | None:
        seg = self.map_widget.segment
        idx = self.map_widget._selected_switch
        if not seg or idx < 0 or idx >= len(seg.switches):
            return None
        return seg.switches[idx]

    def _commit_func_change(self, sw: Switch, select_row: int):
        """Rebuild raw text and refresh the UI after editing sw.funcs."""
        sw.rebuild_raw_text()
        self._on_switch_selected(self.map_widget._selected_switch)
        self.switch_func_list.setCurrentRow(select_row)
        self.map_widget.update()
        self.map_widget.segment_modified.emit()

    def _edit_func(self, item):
        sw = self._current_switch()
        if not sw:
            return
        row = self.switch_func_list.row(item)
        if row < 0 or row >= len(sw.funcs):
            return
        func = self._prompt_switch_func("Edit Switch Function",
                                        sw.funcs[row].to_text())
        if func is None:
            return
        self.map_widget._push_undo()
        sw.funcs[row] = func
        self._commit_func_change(sw, row)

    def _add_custom_func(self):
        sw = self._current_switch()
        if not sw:
            return
        func = self._prompt_switch_func("Add Switch Function", "")
        if func is None:
            return
        self.map_widget._push_undo()
        sw.funcs.append(func)
        self._commit_func_change(sw, len(sw.funcs) - 1)

    def _remove_selected_func(self):
        seg = self.map_widget.segment
        idx = self.map_widget._selected_switch
        if not seg or idx < 0 or idx >= len(seg.switches):
            return
        func_row = self.switch_func_list.currentRow()
        sw = seg.switches[idx]
        if func_row < 0 or func_row >= len(sw.funcs):
            return
        self.map_widget._push_undo()
        sw.funcs.pop(func_row)
        sw.rebuild_raw_text()
        self._on_switch_selected(idx)
        self.map_widget.update()
        self.map_widget.segment_modified.emit()

    def _on_mode_changed(self, id: int, checked: bool):
        if checked:
            mode = EditMode(id)
            self.map_widget.edit_mode = mode
            self.map_widget._selected_switch = -1
            self.map_widget.cancel_target_pick()
            self._pending_func_name = None
            self._on_switch_selected(-1)
            self._show_mode_page(mode)
            self.map_widget.cancel_paste()
            self.map_widget.clear_selection()
            self._hovered_room_text = self._hovered_problem_text = ""
            self._selection_text = self._hovered_tile_text = ""
            self.hover_status.setText("")
            self.tile_palette.show_selection = (mode == EditMode.PAINT)
            self.tile_palette.update()
            self.map_widget.update()

    def _show_mode_page(self, mode: EditMode):
        """Show the side panels for `mode` (if any) and hide the others."""
        for m, pages in self._mode_pages.items():
            for page in pages:
                page.setVisible(m == mode)

    def _delete_all_rooms(self):
        seg = self.map_widget.segment
        if not seg or not seg.rooms:
            return
        self.map_widget._push_undo()  # undoable, so no confirmation needed
        seg.rooms = []
        self.map_widget.update()
        self.map_widget.segment_modified.emit()

    def _auto_rooms(self):
        seg = self.map_widget.segment
        if not seg:
            return
        # Existing rooms are always kept; only the uncovered areas are
        # filled in (use Delete All Rooms first to start from scratch).
        keep = list(seg.rooms)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            new_rooms = auto_rooms(seg, self.tile_table, keep=keep)
        finally:
            QApplication.restoreOverrideCursor()
        self.map_widget._push_undo()
        seg.rooms = keep + new_rooms
        self.map_widget.update()
        self.map_widget.segment_modified.emit()

    def _on_name_changed(self):
        if self._name_warning_open:
            return
        seg = self.map_widget.segment
        if not seg:
            return
        new_name = self.name_edit.text()
        if new_name == seg.name:
            return
        problem = None
        if not new_name:
            problem = "Segment name cannot be empty."
        elif any(s is not seg and s.name == new_name for s in self.segments):
            problem = (f"A segment named '{new_name}' already exists. "
                       "Segment names must be unique.")
        if problem:
            self._name_warning_open = True
            try:
                QMessageBox.warning(self, "Invalid Segment Name", problem)
            finally:
                self._name_warning_open = False
            self.name_edit.setText(seg.name)
            return
        seg.name = new_name
        row = self.seg_list.currentRow()
        if row >= 0:
            self.seg_list.item(row).setText(seg.name)
        self.unsaved = True

    def _on_segment_modified(self):
        self.unsaved = True
        self._update_info()
        self._update_room_check()

    def _update_room_check(self):
        """Re-run the room validator and refresh the Room Check panel and
        the map's problem markers."""
        seg = self.map_widget.segment
        self._check_problems = check_rooms(seg, self.tile_table) if seg else []
        if not seg:
            self.check_label.setText("")
        elif not self._check_problems:
            self.check_label.setText("No problems found.")
        else:
            n = len(self._check_problems)
            self.check_label.setText(f"{n} problem{'s' if n != 1 else ''} found.")
        # "Show on map" stays available even with no problems, so it can
        # be ticked in advance of edits that might introduce some.
        self.map_widget.set_problems(self._check_problems,
                                     self.show_problems_check.isChecked())

    def _on_show_problems_toggled(self, checked: bool):
        set_show_room_problems(checked)
        self.map_widget.set_problems(self._check_problems, checked)

    def _update_info(self):
        """Refresh the Info panel and the size fields (the size may have
        changed through undo/redo of a resize)."""
        seg = self.map_widget.segment
        self._show_size(seg)
        if seg:
            self.info_label.setText(
                f"Rooms: {len(seg.rooms)}\n"
                f"Switches: {len(seg.switches)}"
            )
        else:
            self.info_label.setText("")
