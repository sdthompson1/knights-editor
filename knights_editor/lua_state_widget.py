"""Lua state browser: a tree of the loaded mod's Lua globals, with a
properties panel for the selected value.

The tree is populated lazily (children are fetched when a node is
expanded), which also makes cyclic tables harmless. All introspection
goes through lua_inspect.LuaInspector; this module is only presentation.
"""

import html
import os

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter, QTreeWidget,
    QTreeWidgetItem, QLineEdit, QLabel, QScrollArea, QFormLayout,
    QGridLayout, QSizePolicy, QFrame, QToolButton, QComboBox, QSpinBox,
)
from PySide6.QtGui import QPixmap, QPainter, QColor, QIcon, QFont, QPen
from PySide6.QtCore import Qt, Signal, QSize, QRect

from knights_editor.lua_inspect import (
    LuaInspector, Node, kind_of, is_lua_table,
    KIND_TABLE, KIND_FUNCTION, KIND_STRING, KIND_NUMBER, KIND_BOOLEAN,
    KIND_NIL, KIND_BUILTIN, KIND_SEGMENTS, KIND_SEGMENT, KIND_OTHER,
)
from knights_editor.mod_loader import ModLoader, KtsObject, KtsGraphic, KtsSound
from knights_editor.graphics_cache import GraphicsCache
from knights_editor.anim_preview import AnimPreviewPanel, frame_label

_FACING_CAPTIONS = ("N", "E", "S", "W")

NODE_ROLE = Qt.ItemDataRole.UserRole
POPULATED_ROLE = Qt.ItemDataRole.UserRole + 1

KIND_NAMESPACE = 'namespace'
KIND_GROUP = 'group'

# Glyph and colour for the generated kind icons.
_ICON_STYLE = {
    KIND_TABLE: ("{}", "#5b7fb5"),
    KIND_FUNCTION: ("f", "#8e5bb5"),
    KIND_BUILTIN: ("f", "#9a9a9a"),
    KIND_STRING: ("\"", "#4f9d5a"),
    KIND_NUMBER: ("#", "#d08a2e"),
    KIND_BOOLEAN: ("b", "#2ea3a6"),
    KIND_NIL: ("-", "#888888"),
    KIND_SEGMENTS: ("▦", "#6b8e23"),
    KIND_SEGMENT: ("▦", "#6b8e23"),
    KIND_OTHER: ("?", "#888888"),
    KIND_NAMESPACE: ("ns", "#3b5f8f"),
    KIND_GROUP: ("", "#00000000"),
    'Graphic': ("G", "#c1553d"),
    'Tile': ("T", "#b5892e"),
    'Anim': ("A", "#a0469b"),
    'ItemType': ("I", "#3f8fc4"),
    'MonsterType': ("M", "#b0343a"),
    'Sound': ("S", "#2d9c78"),
    'Overlay': ("O", "#7c6bc4"),
    'Control': ("C", "#5d6d7e"),
}
_DEFAULT_ICON_STYLE = ("K", "#777777")

_glyph_icons: dict[str, QIcon] = {}


def glyph_icon(kind: str) -> QIcon:
    """A small generated icon for a value kind."""
    icon = _glyph_icons.get(kind)
    if icon is not None:
        return icon
    glyph, colour = _ICON_STYLE.get(kind, _DEFAULT_ICON_STYLE)
    pm = QPixmap(16, 16)
    pm.fill(Qt.GlobalColor.transparent)
    if glyph:
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(colour))
        painter.drawRoundedRect(0, 0, 16, 16, 3, 3)
        font = QFont()
        font.setBold(True)
        font.setPixelSize(9 if len(glyph) > 1 else 11)
        painter.setFont(font)
        painter.setPen(QColor("white"))
        painter.drawText(QRect(0, 0, 16, 16), Qt.AlignmentFlag.AlignCenter,
                         glyph)
        painter.end()
    icon = QIcon(pm)
    _glyph_icons[kind] = icon
    return icon


class GraphicPreview(QWidget):
    """Pixel-exact zoomed view of one graphic over a checkerboard, with
    the handle position marked. Emits clicked() when pressed."""

    clicked = Signal()

    def __init__(self, pixmap: QPixmap, handle=(0, 0), scale=4,
                 tooltip: str = "", clickable=False, parent=None):
        super().__init__(parent)
        self._pm = pixmap
        self._handle = handle
        self._scale = scale
        # Shown bounds in image pixels: the image rect extended (if
        # needed) to include the handle pixel, so a handle outside the
        # image is still visible.
        hx, hy = handle
        self._left = min(0, hx)
        self._top = min(0, hy)
        self._right = max(pixmap.width(), hx + 1)
        self._bottom = max(pixmap.height(), hy + 1)
        self._clickable = clickable
        if tooltip:
            self.setToolTip(tooltip)
        if clickable:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def sizeHint(self) -> QSize:
        return QSize((self._right - self._left) * self._scale + 2,
                     (self._bottom - self._top) * self._scale + 2)

    def paintEvent(self, event):
        painter = QPainter(self)
        w = (self._right - self._left) * self._scale
        h = (self._bottom - self._top) * self._scale
        # Offset of the image's top-left within the widget (nonzero only
        # when the handle lies above/left of the image).
        ox, oy = 1 - self._left * self._scale, 1 - self._top * self._scale
        iw = self._pm.width() * self._scale
        ih = self._pm.height() * self._scale
        extended = (w, h) != (iw, ih)
        if extended:
            # Flat fill for the area outside the image proper.
            painter.fillRect(1, 1, w, h, QColor(178, 170, 170))
        # Checkerboard so transparent (colour-keyed) pixels are visible
        cell = 8
        light, dark = QColor(200, 200, 200), QColor(150, 150, 150)
        for y in range(0, ih, cell):
            for x in range(0, iw, cell):
                painter.fillRect(
                    ox + x, oy + y, min(cell, iw - x), min(cell, ih - y),
                    light if ((x // cell + y // cell) % 2 == 0) else dark)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform,
                              False)
        painter.drawPixmap(QRect(ox, oy, iw, ih), self._pm)
        if extended:
            painter.setPen(QPen(QColor(120, 120, 120), 1))
            painter.drawRect(ox - 1, oy - 1, iw + 1, ih + 1)
        hx, hy = self._handle
        painter.setPen(QPen(QColor(255, 40, 40), 1))
        px, py = ox + hx * self._scale, oy + hy * self._scale
        painter.drawLine(px - 6, py, px + 6, py)
        painter.drawLine(px, py - 6, px, py + 6)
        painter.setPen(QPen(QColor(90, 90, 90), 1))
        painter.drawRect(0, 0, w + 1, h + 1)
        painter.end()

    def mousePressEvent(self, event):
        if self._clickable and event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()


class _WrapAwareWidget(QWidget):
    """Container whose minimum height follows its layout's
    height-for-width, so word-wrapped labels inside a resizable
    QScrollArea get the room they need instead of one line."""

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.update_min_height()

    def update_min_height(self):
        lay = self.layout()
        if lay is None:
            return
        lay.activate()
        if lay.hasHeightForWidth():
            self.setMinimumHeight(lay.heightForWidth(self.width()))
        else:
            self.setMinimumHeight(0)


class PropertiesPanel(QScrollArea):
    """Shows details of one Lua value (object, table, function...)."""

    navigate_requested = Signal(object)       # a value to show/select
    locate_requested = Signal(object)         # a value to find in the tree
    open_location = Signal(str, int)          # file path, 1-based line
    open_segment = Signal(object)             # a Segment to open for editing

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self._inspector: LuaInspector | None = None
        self._gfx_cache: GraphicsCache | None = None
        self._history: list[tuple[object, str]] = []
        self._current: tuple[object, str] | None = None
        self._sound_player = None    # created on first use
        self._sound_status = None    # status label of the row last played

        self._inner = _WrapAwareWidget()
        self._layout = QVBoxLayout(self._inner)
        self._layout.setContentsMargins(6, 4, 6, 6)
        self._layout.setSpacing(6)
        self._layout.addStretch()
        self.setWidget(self._inner)
        self.clear()

    def set_context(self, inspector: LuaInspector | None,
                    gfx_cache: GraphicsCache | None):
        self._inspector = inspector
        self._gfx_cache = gfx_cache
        self._history.clear()
        self._current = None
        self.clear()

    def clear(self):
        self._clear_layout()
        hint = QLabel("Select an item in the tree to see its properties.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray;")
        self._layout.insertWidget(0, hint)

    def _clear_layout(self):
        if self._sound_player is not None:
            self._sound_player.stop()
        self._sound_status = None
        while self._layout.count() > 1:   # keep the trailing stretch
            self._dispose(self._layout.takeAt(0))

    def _dispose(self, item):
        """Remove a layout item's widget(s) immediately. Widgets are
        reparented away before deleteLater so they stop being painted
        now rather than after the event loop runs."""
        w = item.widget()
        if w is not None:
            w.hide()
            w.setParent(None)
            w.deleteLater()
        elif item.layout() is not None:
            sub = item.layout()
            while sub.count():
                self._dispose(sub.takeAt(0))
            sub.deleteLater()

    # --- public ---------------------------------------------------------

    def show_value(self, value, label: str, push: bool = True):
        if self._inspector is None:
            return
        if push and self._current is not None \
                and self._current[0] is not value:
            self._history.append(self._current)
            del self._history[:-50]
        self._current = (value, label)
        self._build(value, label)

    def go_back(self):
        if not self._history:
            return
        value, label = self._history.pop()
        self.show_value(value, label, push=False)

    # --- building -------------------------------------------------------

    def _add(self, widget_or_layout):
        idx = self._layout.count() - 1
        if isinstance(widget_or_layout, QWidget):
            self._layout.insertWidget(idx, widget_or_layout)
        else:
            self._layout.insertLayout(idx, widget_or_layout)

    def _build(self, value, label: str):
        ins = self._inspector
        self._clear_layout()
        kind = kind_of(value)

        # Header: back button, icon, name, kind
        header = QHBoxLayout()
        back = QToolButton()
        back.setArrowType(Qt.ArrowType.LeftArrow)
        back.setToolTip("Back")
        back.setEnabled(bool(self._history))
        back.clicked.connect(self.go_back)
        header.addWidget(back)
        icon_lbl = QLabel()
        icon_lbl.setPixmap(glyph_icon(kind).pixmap(16, 16))
        header.addWidget(icon_lbl)
        name = ins.global_name(value) if isinstance(value, KtsObject) else None
        title = QLabel(f"<b>{html.escape(name or label)}</b>")
        title.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        header.addWidget(title)
        kl = QLabel(kind)
        kl.setStyleSheet("color: gray;")
        header.addWidget(kl)
        header.addStretch()
        locate_lbl = QLabel("Locate:")
        locate_lbl.setStyleSheet("color: gray;")
        header.addWidget(locate_lbl)
        tree_btn = QToolButton()
        tree_btn.setText("Tree")
        tree_btn.setAutoRaise(True)
        tree_btn.setToolTip("Select this item in the tree")
        tree_btn.clicked.connect(
            lambda _=None, v=value: self.locate_requested.emit(v))
        header.addWidget(tree_btn)
        code_btn = QToolButton()
        code_btn.setText("Code")
        code_btn.setAutoRaise(True)
        defn = ins.definition_source(value, label)
        if defn is not None:
            path, line = defn
            code_btn.setToolTip("Go to this item's definition "
                                f"({os.path.basename(path)}:{line})")
            code_btn.clicked.connect(
                lambda _=None, p=path, ln=line: self.open_location.emit(p, ln))
        else:
            code_btn.setEnabled(False)
            code_btn.setToolTip("No source location known for this item")
        header.addWidget(code_btn)
        self._add(header)

        aliases, refs = ins.find_references(value)
        others = [a for a in aliases if a != name]
        if others:
            al = QLabel("Also: " + ", ".join(html.escape(a) for a in others))
            al.setWordWrap(True)
            al.setStyleSheet("color: gray;")
            self._add(al)

        # Kind-specific previews
        if isinstance(value, KtsGraphic):
            self._add_graphic_preview(value)
        elif isinstance(value, KtsObject):
            if value.kind == 'Anim':
                self._add(AnimPreviewPanel(value, ins, self._gfx_cache))
                self._add_anim_frames(value)
            elif value.kind == 'Overlay':
                self._add_frames(value.graphics(), per_row=4,
                                 captions=_FACING_CAPTIONS)
            elif isinstance(value, KtsSound):
                self._add_sound_preview(value)
            else:
                self._add_object_previews(value)
        elif kind == KIND_FUNCTION:
            self._add_function_info(value)
        elif kind == KIND_STRING:
            text = value.decode('utf-8', 'replace') \
                if isinstance(value, bytes) else value
            lbl = QLabel(text)
            lbl.setTextFormat(Qt.TextFormat.PlainText)
            lbl.setWordWrap(True)
            lbl.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse)
            lbl.setFrameShape(QFrame.Shape.StyledPanel)
            self._add(lbl)

        # Property rows
        rows = ins.properties(value)
        if rows:
            form = QFormLayout()
            form.setContentsMargins(0, 4, 0, 0)
            form.setHorizontalSpacing(10)
            form.setVerticalSpacing(3)
            form.setLabelAlignment(Qt.AlignmentFlag.AlignRight
                                   | Qt.AlignmentFlag.AlignTop)
            form.setFieldGrowthPolicy(
                QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            for row_label, row_value in rows:
                form.addRow(html.escape(row_label) + ":",
                            self._value_widget(row_value, row_label))
            self._add(form)
            if is_lua_table(value) and len(rows) >= 200:
                note = QLabel("(first 200 entries shown; expand the tree "
                              "node for all)")
                note.setStyleSheet("color: gray;")
                self._add(note)

        if kind == KIND_SEGMENT:
            self._add(self._segment_link("Open in editor", value))

        # References
        if refs:
            self._add(self._section_label("Referenced by"))
            for ref in refs:
                self._add(self._link(ref.text, ref.holder))
        elif isinstance(value, KtsObject) and value.kind in (
                'Graphic', 'Tile', 'Anim', 'ItemType', 'MonsterType',
                'Sound', 'Overlay'):
            note = QLabel("Not referenced by any global")
            note.setStyleSheet("color: gray;")
            self._add(note)

        self._inner.update_min_height()
        self.verticalScrollBar().setValue(0)

    def _section_label(self, text: str) -> QLabel:
        lbl = QLabel(f"<b>{html.escape(text)}</b>")
        lbl.setContentsMargins(0, 6, 0, 0)
        return lbl

    def _link(self, text: str, target, tooltip: str = "") -> QLabel:
        lbl = QLabel(f'<a href="#">{html.escape(text)}</a>')
        lbl.setTextFormat(Qt.TextFormat.RichText)
        lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.LinksAccessibleByMouse)
        if tooltip:
            lbl.setToolTip(tooltip)
        lbl.linkActivated.connect(
            lambda _=None, t=target: self.navigate_requested.emit(t))
        return lbl

    def _segment_link(self, text: str, segment) -> QLabel:
        """A link that opens `segment` in the segment editor."""
        lbl = QLabel(f'<a href="#">{html.escape(text)}</a>')
        lbl.setTextFormat(Qt.TextFormat.RichText)
        lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.LinksAccessibleByMouse)
        src = getattr(segment, 'source_file', None)
        lbl.setToolTip("Open in the segment editor"
                       + (f" ({os.path.basename(src)})" if src else ""))
        lbl.linkActivated.connect(
            lambda _=None, s=segment: self.open_segment.emit(s))
        return lbl

    def _source_link(self, path: str, line: int) -> QLabel:
        text = f"{os.path.basename(path)}:{line}"
        lbl = QLabel(f'<a href="#">{html.escape(text)}</a>')
        lbl.setTextFormat(Qt.TextFormat.RichText)
        lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.LinksAccessibleByMouse)
        lbl.setToolTip(path)
        lbl.linkActivated.connect(
            lambda _=None: self.open_location.emit(path, line))
        return lbl

    def _value_widget(self, value, label: str) -> QWidget:
        ins = self._inspector
        kind = kind_of(value)
        if kind == KIND_SEGMENT:
            return self._segment_link(ins.summary(value, label), value)
        if isinstance(value, KtsObject) or is_lua_table(value) \
                or kind == KIND_SEGMENTS:
            return self._link(ins.summary(value, label), value)
        if kind == KIND_FUNCTION:
            loc = ins.function_source(value)
            if loc:
                return self._source_link(*loc)
            return QLabel("function")
        if isinstance(value, tuple) and len(value) == 3 \
                and label == "Colour key":
            return self._colour_swatch(value)
        if isinstance(value, str):
            lbl = QLabel(value if len(value) <= 200 else value[:199] + "…")
            lbl.setWordWrap(True)
            if len(value) > 200:
                # Tool tips auto-detect rich text too, so escape and force
                # rich text (pre-wrap keeps newlines/spacing intact).
                lbl.setToolTip('<span style="white-space: pre-wrap">'
                               + html.escape(value) + '</span>')
        else:
            lbl = QLabel(ins.summary(value, label))
        # Lua strings are arbitrary text; never let Qt sniff them as HTML.
        lbl.setTextFormat(Qt.TextFormat.PlainText)
        lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        return lbl

    def _colour_swatch(self, rgb) -> QWidget:
        r, g, b = rgb
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        pm = QPixmap(14, 14)
        pm.fill(QColor(r, g, b))
        painter = QPainter(pm)
        painter.setPen(QColor(60, 60, 60))
        painter.drawRect(0, 0, 13, 13)
        painter.end()
        sw = QLabel()
        sw.setPixmap(pm)
        lay.addWidget(sw)
        lay.addWidget(QLabel(f"({r}, {g}, {b})"))
        lay.addStretch()
        return w

    def _pixmap_for(self, graphic: KtsGraphic) -> QPixmap | None:
        if self._gfx_cache is None:
            return None
        pm = self._gfx_cache.get_graphic_pixmap(graphic.to_model())
        return pm if not pm.isNull() else None

    def _preview_scale(self, pm: QPixmap, max_px: int) -> int:
        longest = max(pm.width(), pm.height(), 1)
        return max(1, min(4, max_px // longest))

    def _add_graphic_preview(self, graphic: KtsGraphic):
        pm = self._pixmap_for(graphic)
        row = QHBoxLayout()
        if pm is None:
            lbl = QLabel("(image not found)")
            lbl.setStyleSheet("color: #aa0000;")
            row.addWidget(lbl)
        else:
            prev = GraphicPreview(pm, graphic.handle_pos,
                                  self._preview_scale(pm, 160))
            row.addWidget(prev)
            hx, hy = graphic.handle_pos
            info = QLabel(f"{pm.width()} × {pm.height()} px\n"
                          f"handle ({hx}, {hy})")
            info.setStyleSheet("color: gray;")
            row.addWidget(info, 0, Qt.AlignmentFlag.AlignBottom)
        row.addStretch()
        self._add(row)

    def _add_sound_preview(self, sound: KtsSound):
        """Play controls for a Sound: a Play button and a frequency box,
        so the sound can be heard at any rate kts.PlaySound might use
        (the game plays the samples at that rate, ignoring the file's
        own rate)."""
        from knights_editor.sound_player import WavData, SoundPlayer
        row = QHBoxLayout()
        row.setSpacing(6)
        try:
            wav = WavData(sound.filename)
        except FileNotFoundError:
            lbl = QLabel("(sound file not found)")
            lbl.setStyleSheet("color: #aa0000;")
            row.addWidget(lbl)
            row.addStretch()
            self._add(row)
            return
        except Exception as e:
            lbl = QLabel(f"(cannot read WAV file: {e})")
            lbl.setStyleSheet("color: #aa0000;")
            lbl.setWordWrap(True)
            row.addWidget(lbl)
            row.addStretch()
            self._add(row)
            return
        if self._sound_player is None:
            self._sound_player = SoundPlayer(self)
            self._sound_player.playback_error.connect(self._on_sound_error)
        play = QToolButton()
        play.setText("▶ Play")
        stop = QToolButton()
        stop.setText("■")
        stop.setToolTip("Stop playback")
        freq = QSpinBox()
        freq.setRange(1000, 96000)
        freq.setSingleStep(500)
        freq.setSuffix(" Hz")
        freq.setValue(wav.native_rate)
        freq.setToolTip(
            "Playback frequency, as passed to kts.PlaySound. The game "
            "plays the raw samples at this rate; the file's own rate "
            f"({wav.native_rate} Hz) is only used as the default here.")
        status = QLabel()
        status.setStyleSheet("color: #aa0000;")
        info = QLabel()
        info.setStyleSheet("color: gray;")

        def update_info():
            secs = wav.n_frames / freq.value()
            ch = "mono" if wav.channels == 1 else f"{wav.channels}-channel"
            info.setText(f"{wav.sample_width * 8}-bit {ch}, "
                         f"{wav.n_frames} samples = {secs:.2f} s")

        freq.valueChanged.connect(lambda _=None: update_info())
        update_info()
        def on_play():
            self._sound_status = status
            status.setText(self._sound_player.play(wav, freq.value()) or "")

        play.clicked.connect(on_play)
        stop.clicked.connect(self._sound_player.stop)
        row.addWidget(play)
        row.addWidget(stop)
        row.addWidget(freq)
        row.addWidget(info)
        row.addWidget(status)
        row.addStretch()
        self._add(row)

    def _on_sound_error(self, msg: str):
        """A device error the audio backend reported after play()
        returned; show it on the row that started the playback."""
        if self._sound_status is not None:
            self._sound_status.setText(msg)

    def _frame_widget(self, graphic: KtsGraphic, scale: int,
                      caption: str) -> QWidget:
        """A small clickable preview of one graphic, with a caption."""
        pm = self._pixmap_for(graphic)
        name = self._inspector.global_name(graphic) or \
            os.path.basename(graphic.raw_filename)
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(1)
        if pm is None:
            missing = QLabel("?")
            missing.setToolTip(f"{name}: image not found")
            lay.addWidget(missing, 0, Qt.AlignmentFlag.AlignHCenter)
        else:
            prev = GraphicPreview(pm, graphic.handle_pos, scale,
                                  tooltip=name, clickable=True)
            prev.clicked.connect(
                lambda g=graphic: self.navigate_requested.emit(g))
            lay.addWidget(prev, 0, Qt.AlignmentFlag.AlignHCenter)
        if caption:
            cap = QLabel(caption)
            cap.setStyleSheet("color: gray; font-size: 8pt;")
            lay.addWidget(cap, 0, Qt.AlignmentFlag.AlignHCenter)
        return box

    def _add_frames(self, frames: list[KtsGraphic], per_row: int,
                    captions: tuple[str, ...] | None = None):
        if not frames:
            return
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(4)
        grid.setVerticalSpacing(4)
        for i, g in enumerate(frames):
            caption = captions[i % per_row] if captions else str(i + 1)
            grid.addWidget(self._frame_widget(g, 2, caption),
                           i // per_row, i % per_row)
        grid.setColumnStretch(per_row, 1)
        self._add(grid)

    def _add_anim_frames(self, anim: KtsObject):
        """The anim's frames as a facing (column) x frame (row) grid,
        with each row labelled by the frame's meaning in the game. Bat
        anims are one facing-less row of flap/bite frames."""
        frames = anim.graphics()
        if not frames:
            return
        if anim.props.get('bat'):
            self._add_frames(frames, per_row=4, captions=tuple(
                frame_label(i, len(frames), bat=True)
                for i in range(min(4, len(frames)))))
            return
        n_frames = max(1, len(frames) // 4)
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(4)
        grid.setVerticalSpacing(4)
        for i, g in enumerate(frames):
            grid.addWidget(self._frame_widget(g, 2, _FACING_CAPTIONS[i % 4]),
                           i // 4, i % 4)
        for row in range(n_frames):
            lbl = QLabel(frame_label(row, n_frames))
            lbl.setStyleSheet("color: gray;")
            grid.addWidget(lbl, row, 4, Qt.AlignmentFlag.AlignVCenter)
        grid.setColumnStretch(5, 1)
        self._add(grid)

    def _add_object_previews(self, obj: KtsObject):
        """Previews for Tile / ItemType / MonsterType / Control etc.: the
        graphic-valued props, then any anim/overlay frame strips."""
        graphic_props = [(k, v) for k, v in obj.props.items()
                         if isinstance(k, str) and isinstance(v, KtsGraphic)]
        priority = ['graphic', 'open_graphic', 'stack_graphic',
                    'backpack_graphic', 'backpack_overdraw', 'menu_icon']
        graphic_props.sort(key=lambda kv: (
            priority.index(kv[0]) if kv[0] in priority else len(priority),
            kv[0]))
        if graphic_props:
            row = QHBoxLayout()
            for k, g in graphic_props:
                row.addWidget(self._frame_widget(g, 3, k))
            row.addStretch()
            self._add(row)
        for k, v in sorted(obj.props.items(), key=lambda kv: str(kv[0])):
            if isinstance(v, KtsObject) and v.kind in ('Anim', 'Overlay'):
                frames = v.graphics()
                if not frames:
                    continue
                name = self._inspector.global_name(v) or v.kind
                self._add(self._link(f"{k}: {name}", v))
                if v.kind == 'Anim':
                    self._add_anim_frames(v)
                else:
                    self._add_frames(frames, per_row=4,
                                     captions=_FACING_CAPTIONS)

    def _add_function_info(self, fn):
        loc = self._inspector.function_source(fn)
        row = QHBoxLayout()
        if loc is None:
            lbl = QLabel("Defined in Lua (no source location available)")
            lbl.setStyleSheet("color: gray;")
            row.addWidget(lbl)
        else:
            row.addWidget(QLabel("Defined at"))
            row.addWidget(self._source_link(*loc))
        row.addStretch()
        self._add(row)


class LuaStateWidget(QWidget):
    """Sidebar tab: filterable tree of loaded Lua state + properties."""

    open_location = Signal(str, int)   # file path, 1-based line
    open_segment = Signal(object)      # a Segment to open for editing

    def __init__(self, parent=None):
        super().__init__(parent)
        self._inspector: LuaInspector | None = None
        self._gfx_cache: GraphicsCache | None = None
        self._mod_name: str | None = None
        self._building = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        controls = QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(4)
        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Filter globals…")
        self._filter.setClearButtonEnabled(True)
        self._filter.textChanged.connect(self._apply_filter)
        controls.addWidget(self._filter, 1)
        self._sort_combo = QComboBox()
        self._sort_combo.addItems(["Sort by name", "Sort by type"])
        self._sort_combo.setToolTip(
            "Order of entries in the tree: alphabetical, or grouped by "
            "type (as shown by the icon) then alphabetical")
        self._sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        controls.addWidget(self._sort_combo)
        layout.addLayout(controls)

        splitter = QSplitter(Qt.Orientation.Vertical)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(["Name", "Value"])
        self.tree.setUniformRowHeights(True)
        self.tree.setIconSize(QSize(16, 16))
        self.tree.header().setStretchLastSection(True)
        self.tree.setColumnWidth(0, 185)
        self.tree.itemExpanded.connect(self._on_expanded)
        self.tree.currentItemChanged.connect(self._on_current_changed)
        self.tree.itemActivated.connect(self._on_activated)
        splitter.addWidget(self.tree)

        self.panel = PropertiesPanel()
        self.panel.navigate_requested.connect(self.navigate_to)
        self.panel.locate_requested.connect(self.locate)
        self.panel.open_location.connect(self.open_location)
        self.panel.open_segment.connect(self.open_segment)
        self.panel.setMinimumHeight(120)
        splitter.addWidget(self.panel)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([420, 280])
        layout.addWidget(splitter)

        self._show_empty()

    # --- state ----------------------------------------------------------

    def _show_empty(self):
        self.tree.clear()
        item = QTreeWidgetItem(["(no mod loaded)"])
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        self.tree.addTopLevelItem(item)

    def clear(self):
        self._inspector = None
        self._gfx_cache = None
        self.panel.set_context(None, None)
        self._show_empty()

    def set_state(self, loader: ModLoader, mod_dir: str,
                  gfx_cache: GraphicsCache | None):
        """Show the state held by `loader` (which may be partial, if the
        load failed; errors are reported by the main window). Expanded
        nodes and the selection are restored by path where the same names
        still exist (a reload replaces every object)."""
        mod_name = os.path.basename(os.path.normpath(mod_dir))
        had_state = self._inspector is not None
        self._mod_name = mod_name
        self._inspector = LuaInspector(
            loader, mod_name, sort_by_type=self._sort_by_type())
        self._gfx_cache = gfx_cache
        self.panel.set_context(self._inspector, gfx_cache)
        self._rebuild_preserving(restore=had_state)

    def _sort_by_type(self) -> bool:
        return self._sort_combo.currentIndex() == 1

    def _on_sort_changed(self, index: int):
        if self._inspector is None:
            return
        self._inspector.sort_by_type = self._sort_by_type()
        self._rebuild_preserving(restore=True)

    def _rebuild_preserving(self, restore: bool):
        """Rebuild the tree. With restore=True, expanded nodes and the
        selection are put back by path where the same names still exist
        (a reload replaces every object, so identity can't be used)."""
        expanded = self._expanded_paths() if restore else None
        selected = self._item_path(self.tree.currentItem()) \
            if restore else None
        self._rebuild()
        if expanded:
            for path in expanded:
                self._expand_path(path)
        else:
            root = self.tree.topLevelItem(0)
            if root is not None:
                root.setExpanded(True)
        if selected:
            item = self._find_path(selected)
            if item is not None:
                self.tree.setCurrentItem(item)
                self.tree.scrollToItem(item)
        if self._filter.text():
            self._apply_filter(self._filter.text())

    # --- tree building --------------------------------------------------

    def _rebuild(self):
        ins = self._inspector
        self._building = True
        try:
            self.tree.clear()
            spaces = ins.namespaces()
            globals_table = ins.globals_table()
            own = [(n, ns) for n, ns in spaces if n == self._mod_name]
            deps = [(n, ns) for n, ns in spaces if n != self._mod_name]

            own_is_globals = False
            if own:
                name, ns = own[0]
                own_is_globals = ins.same_value(ns, globals_table)
                self._add_root(self._namespace_node(name, ns), populate=True)
            if deps:
                group = self._add_root(Node(
                    key=None, label="Required modules", kind=KIND_GROUP,
                    summary="", value=None, expandable=True))
                group.setData(0, POPULATED_ROLE, True)
                for name, ns in deps:
                    self._make_item(group, self._namespace_node(name, ns))
            if not own_is_globals:
                self._add_root(Node(
                    key=None, label="Lua globals (_G)", kind=KIND_NAMESPACE,
                    summary="", value=globals_table, expandable=True))
        finally:
            self._building = False

    def _namespace_node(self, name: str, ns) -> Node:
        n = 0
        try:
            for _ in ns.items():
                n += 1
        except Exception:
            pass
        return Node(key=name, label=name, kind=KIND_NAMESPACE,
                    summary=f"{n} global{'s' if n != 1 else ''}", value=ns,
                    expandable=n > 0)

    def _add_root(self, node: Node, populate=False) -> QTreeWidgetItem:
        item = self._make_item(None, node)
        self.tree.addTopLevelItem(item)
        if populate:
            self._populate(item)
        return item

    def _make_item(self, parent, node: Node) -> QTreeWidgetItem:
        item = QTreeWidgetItem([node.label, node.summary])
        item.setData(0, NODE_ROLE, node)
        item.setData(0, POPULATED_ROLE, False)
        item.setIcon(0, self._icon_for(node))
        item.setToolTip(0, node.label)
        item.setToolTip(1, node.summary)
        if node.kind == KIND_GROUP:
            font = item.font(0)
            font.setBold(True)
            item.setFont(0, font)
        if node.expandable:
            # Placeholder so the expand arrow shows; replaced on expand.
            QTreeWidgetItem(item, ["…"])
        if parent is not None:
            parent.addChild(item)
        return item

    def _icon_for(self, node: Node) -> QIcon:
        # Always the kind glyph (never a graphic preview), so a Tile and
        # the Graphic it uses are visually distinct in the tree.
        return glyph_icon(node.kind)

    def _populate(self, item: QTreeWidgetItem):
        if item.data(0, POPULATED_ROLE):
            return
        node: Node = item.data(0, NODE_ROLE)
        item.setData(0, POPULATED_ROLE, True)
        item.takeChildren()
        if node is None or node.value is None:
            return
        for child in self._inspector.children(node.value):
            self._make_item(item, child)

    def _on_expanded(self, item: QTreeWidgetItem):
        if self._building:
            return
        self._populate(item)
        if self._filter.text():
            # Filter the newly fetched children only. The item itself was
            # visible (the user just clicked it) and must stay so even if
            # nothing inside it matches; _filter_item on the item would
            # hide a populated container with no matching children.
            text = self._filter.text().strip().lower()
            for i in range(item.childCount()):
                self._filter_item(item.child(i), text)

    # --- selection / navigation ----------------------------------------

    def _on_current_changed(self, current, previous):
        if self._building or current is None:
            return
        node: Node = current.data(0, NODE_ROLE)
        if node is None or node.kind == KIND_GROUP:
            return
        self.panel.show_value(node.value, node.label)

    def _on_activated(self, item: QTreeWidgetItem, column: int):
        node: Node = item.data(0, NODE_ROLE)
        if node is None or self._inspector is None:
            return
        if node.kind == KIND_FUNCTION:
            loc = self._inspector.function_source(node.value)
            if loc:
                self.open_location.emit(*loc)

    def navigate_to(self, value):
        """Show `value` in the panel, selecting it in the tree if it is a
        named global."""
        if self._inspector is None:
            return
        item = None
        loc = self._inspector.global_location(value)
        if loc is not None:
            item = self._find_global_item(*loc)
        if item is None:
            item = self._find_item_with_value(value)
        if item is not None:
            self.tree.setCurrentItem(item)
            self.tree.scrollToItem(item)
            return
        self.panel.show_value(value, self._inspector.summary(value))

    def locate(self, value):
        """Select and scroll to the tree item holding `value`: the current
        item if it still shows the value, else the global it is bound to,
        else any already-loaded item with the same value."""
        if self._inspector is None:
            return
        item = self.tree.currentItem()
        if item is None or not self._item_holds(item, value):
            item = None
            loc = self._inspector.global_location(value)
            if loc is not None:
                item = self._find_global_item(*loc)
            if item is None:
                item = self._find_item_with_value(value)
        if item is None:
            return
        parent = item.parent()
        while parent is not None:
            parent.setExpanded(True)
            parent = parent.parent()
        self.tree.setCurrentItem(item)
        self.tree.scrollToItem(item, QTreeWidget.ScrollHint.PositionAtCenter)
        self.tree.setFocus()

    def _item_holds(self, item: QTreeWidgetItem, value) -> bool:
        node: Node = item.data(0, NODE_ROLE)
        if node is None or node.value is None:
            return False
        return self._inspector.same_value(node.value, value)

    def _find_item_with_value(self, value):
        """Depth-first search of the already-populated tree."""
        def walk(item):
            if self._item_holds(item, value):
                return item
            if item.data(0, POPULATED_ROLE):
                for i in range(item.childCount()):
                    found = walk(item.child(i))
                    if found is not None:
                        return found
            return None

        for i in range(self.tree.topLevelItemCount()):
            found = walk(self.tree.topLevelItem(i))
            if found is not None:
                return found
        return None

    def _find_global_item(self, mod: str, name: str):
        if mod == self._mod_name:
            path = [mod, name]
        elif mod == "_G":
            path = ["Lua globals (_G)", name]
        else:
            path = ["Required modules", mod, name]
        return self._find_path(path)

    # --- paths (for state restore and navigation) ----------------------

    def _item_path(self, item) -> list[str] | None:
        if item is None:
            return None
        path = []
        while item is not None:
            path.append(item.text(0))
            item = item.parent()
        path.reverse()
        return path

    def _expanded_paths(self) -> list[list[str]]:
        paths = []

        def walk(item, prefix):
            if not item.isExpanded():
                return
            path = prefix + [item.text(0)]
            paths.append(path)
            for i in range(item.childCount()):
                walk(item.child(i), path)

        for i in range(self.tree.topLevelItemCount()):
            walk(self.tree.topLevelItem(i), [])
        return paths

    def _find_path(self, path: list[str]):
        """Locate an item by its label path, populating lazily."""
        if not path:
            return None
        item = None
        for i in range(self.tree.topLevelItemCount()):
            if self.tree.topLevelItem(i).text(0) == path[0]:
                item = self.tree.topLevelItem(i)
                break
        for label in path[1:]:
            if item is None:
                return None
            self._populate(item)
            found = None
            for i in range(item.childCount()):
                if item.child(i).text(0) == label:
                    found = item.child(i)
                    break
            item = found
        return item

    def _expand_path(self, path: list[str]):
        item = self._find_path(path)
        if item is not None:
            self._populate(item)
            item.setExpanded(True)

    # --- filtering ------------------------------------------------------

    def _apply_filter(self, text: str):
        text = text.strip().lower()
        if self._inspector is None:
            return
        if not text:
            for i in range(self.tree.topLevelItemCount()):
                self._unhide(self.tree.topLevelItem(i))
            return
        # Make sure the mod namespaces are populated so their globals
        # can be searched (the _G builtins are left alone).
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            node: Node = top.data(0, NODE_ROLE)
            if node is None:
                continue
            if node.kind == KIND_GROUP:
                for j in range(top.childCount()):
                    self._populate(top.child(j))
            elif node.label != "Lua globals (_G)":
                self._populate(top)
        for i in range(self.tree.topLevelItemCount()):
            self._filter_item(self.tree.topLevelItem(i), text)

    def _unhide(self, item):
        item.setHidden(False)
        for i in range(item.childCount()):
            self._unhide(item.child(i))

    def _filter_item(self, item, text: str) -> bool:
        """Hide items not matching `text` (or containing a match);
        expands containers that hold matches. Returns visibility."""
        node: Node = item.data(0, NODE_ROLE)
        if node is None:
            item.setHidden(True)
            return False
        own_match = text in node.label.lower() or text in node.summary.lower()
        child_match = False
        if item.data(0, POPULATED_ROLE):
            for i in range(item.childCount()):
                if self._filter_item(item.child(i), text):
                    child_match = True
        container = node.kind in (KIND_GROUP, KIND_NAMESPACE)
        visible = child_match or (own_match and not container) \
            or (container and not item.data(0, POPULATED_ROLE))
        item.setHidden(not visible)
        if child_match and container:
            item.setExpanded(True)
        return visible
