"""Tile palette widget for selecting tiles."""

from PySide6.QtWidgets import QWidget
from PySide6.QtGui import QPainter, QColor, QPen
from PySide6.QtCore import Qt, Signal

from knights_editor.models import TileEntry
from knights_editor.graphics_cache import TilePixmapCache, TILE_SIZE
from knights_editor.zooming import next_level


PALETTE_SCALE = 2  # Draw tiles at 2x size (at 100% zoom)

# Mouse buttons that can each carry their own brush tile, as small
# integers used throughout the editor: 1 = left, 2 = right, 3 = middle,
# 4/5 = the "extra" (back/forward) buttons of a 5-button mouse.
BRUSH_BUTTONS: dict[Qt.MouseButton, int] = {
    Qt.MouseButton.LeftButton: 1,
    Qt.MouseButton.RightButton: 2,
    Qt.MouseButton.MiddleButton: 3,
    Qt.MouseButton.XButton1: 4,
    Qt.MouseButton.XButton2: 5,
}
BRUSH_LABELS = {1: "L", 2: "R", 3: "M", 4: "X1", 5: "X2"}
# Colour of the outline drawn round each button's tile in the palette
BRUSH_COLOURS = {
    1: QColor(0, 255, 0),
    2: QColor(0, 100, 255),
    3: QColor(255, 200, 0),
    4: QColor(255, 0, 255),
    5: QColor(0, 220, 220),
}


class TilePaletteWidget(QWidget):
    """Grid of available tiles. Click to select a brush tile for the
    mouse button used (see BRUSH_BUTTONS)."""

    tile_selected = Signal(int, int)  # tile_number, mouse_button (BRUSH_BUTTONS value)
    tile_hovered = Signal(int)        # tile_number under mouse (-1 if none)
    zoom_changed = Signal(float)      # zoom factor (1.0 = 100%)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.tile_table: dict[int, TileEntry] = {}
        self.tile_cache: TilePixmapCache | None = None
        self.zoom = 1.0
        self._sorted_indices: list[int] = []
        self.brush_tiles: dict[int, int] = {}  # button -> tile_number
        # Selection outlines only shown in Paint mode
        self.show_selection = True
        self.setMouseTracking(True)

    def _tile_px(self) -> int:
        """Size in pixels of one tile at the current zoom."""
        return max(1, round(TILE_SIZE * PALETTE_SCALE * self.zoom))

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

    def wheelEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            if event.angleDelta().y() > 0:
                self.zoom_in()
            elif event.angleDelta().y() < 0:
                self.zoom_out()
            event.accept()
        else:
            super().wheelEvent(event)

    def set_tile_table(self, tile_table: dict[int, TileEntry],
                       tile_cache: TilePixmapCache):
        self.tile_table = tile_table
        self.tile_cache = tile_cache
        self._sorted_indices = sorted(tile_table.keys())
        self._update_size()
        self.update()

    def _cols(self) -> int:
        return max(1, self.width() // self._tile_px())

    def _update_size(self):
        if not self._sorted_indices:
            return
        cols = self._cols()
        rows = (len(self._sorted_indices) + cols - 1) // cols
        self.setMinimumHeight(rows * self._tile_px())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_size()
        self.update()

    def _index_at(self, pos) -> int | None:
        s = self._tile_px()
        cols = self._cols()
        col = pos.x() // s
        row = pos.y() // s
        if col >= cols:
            return None
        idx = row * cols + col
        if 0 <= idx < len(self._sorted_indices):
            return self._sorted_indices[idx]
        return None

    def paintEvent(self, event):
        if not self.tile_cache or not self._sorted_indices:
            return

        painter = QPainter(self)
        s = self._tile_px()
        cols = self._cols()

        for i, tile_idx in enumerate(self._sorted_indices):
            col = i % cols
            row = i // cols
            pm = self.tile_cache.get_tile_pixmap(tile_idx)
            painter.drawPixmap(col * s, row * s, s, s, pm)

            # Highlight selected tiles. If several buttons share a tile
            # the outlines are nested so each remains visible.
            if not self.show_selection:
                continue
            inset = 1
            for button in sorted(self.brush_tiles):
                if self.brush_tiles[button] != tile_idx:
                    continue
                painter.setPen(QPen(BRUSH_COLOURS[button], 2))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(col * s + inset, row * s + inset,
                                 s - 2 * inset, s - 2 * inset)
                inset += 2

        painter.end()

    def mousePressEvent(self, event):
        tile_idx = self._index_at(event.position().toPoint())
        button = BRUSH_BUTTONS.get(event.button())
        if tile_idx is not None and button is not None:
            self.tile_selected.emit(tile_idx, button)

    def mouseMoveEvent(self, event):
        tile_idx = self._index_at(event.position().toPoint())
        self.tile_hovered.emit(tile_idx if tile_idx is not None else -1)

    def leaveEvent(self, event):
        self.tile_hovered.emit(-1)
