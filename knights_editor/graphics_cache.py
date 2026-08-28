"""Graphics loading with color key transparency and tile compositing."""

import os
from PySide6.QtGui import QImage, QPixmap, QPainter, QColor, QBitmap
from PySide6.QtCore import Qt
from knights_editor.models import Graphic, TileEntry

TILE_SIZE = 16


class GraphicsCache:
    """Loads and caches graphics files as QPixmaps.

    Shared by every editor for the loaded mod: entries are keyed by
    filename and colour key, which mean the same thing everywhere.
    Composited tile pixmaps live in a TilePixmapCache instead, since
    they depend on which tile table is in use."""

    def __init__(self, mod_base_dir: str):
        """mod_base_dir: path to the module directory (e.g. .../modules/base/)"""
        self.mod_base_dir = mod_base_dir
        self._pixmap_cache: dict[str, QPixmap] = {}

    def get_graphic_pixmap(self, graphic: Graphic) -> QPixmap:
        """Load a single graphic as a QPixmap with color key applied."""
        key_str = f"{graphic.filename}:{graphic.color_key}"
        if key_str in self._pixmap_cache:
            return self._pixmap_cache[key_str]

        # Graphic filenames are normally absolute (resolved by ModLoader
        # against the module that defined them); fall back to the opened
        # mod's directory for any relative ones.
        if os.path.isabs(graphic.filename):
            filepath = graphic.filename
        else:
            filepath = os.path.join(self.mod_base_dir, graphic.filename)
        img = QImage(filepath)
        if img.isNull():
            # Return a placeholder
            pm = QPixmap(TILE_SIZE, TILE_SIZE)
            pm.fill(QColor(255, 0, 255))
            self._pixmap_cache[key_str] = pm
            return pm

        # Convert to ARGB32 for alpha manipulation
        img = img.convertToFormat(QImage.Format.Format_ARGB32)

        pm = QPixmap.fromImage(img)
        # Apply color key transparency: pixels of the key colour become
        # fully transparent. (MaskInColor marks the key pixels in the
        # mono mask; QBitmap.fromImage normalises the mask's colour
        # table such that setMask then clears exactly those pixels -
        # verified against a per-pixel loop on the base mod's graphics.)
        if graphic.color_key is not None:
            r, g, b = graphic.color_key
            mask = img.createMaskFromColor(QColor(r, g, b).rgb(),
                                           Qt.MaskMode.MaskInColor)
            pm.setMask(QBitmap.fromImage(mask))

        self._pixmap_cache[key_str] = pm
        return pm

    def clear_cache(self):
        self._pixmap_cache.clear()


class TilePixmapCache:
    """Composited pixmaps for the entries of one tile table.

    The same tile number can mean different tiles in different per-file
    tile tables, so each table gets its own cache, created alongside it
    and discarded with it. (A shared cache keyed by the table's id()
    would not do: ids are recycled once a table is garbage collected,
    so a later table could be served another table's pixmaps.)
    """

    def __init__(self, tile_table: dict[int, TileEntry], gfx_cache: GraphicsCache):
        self.tile_table = tile_table
        self.gfx_cache = gfx_cache
        self._pixmaps: dict[int, QPixmap] = {}

    def get_tile_pixmap(self, tile_idx: int) -> QPixmap:
        """Get the composited QPixmap for a tile table entry."""
        pm = self._pixmaps.get(tile_idx)
        if pm is not None:
            return pm

        entry = self.tile_table.get(tile_idx)
        if entry is None or not entry.tiles:
            pm = QPixmap(TILE_SIZE, TILE_SIZE)
            pm.fill(QColor(0, 0, 0))
            self._pixmaps[tile_idx] = pm
            return pm

        # Composite all tile graphics (back to front)
        # Higher depth = further back (drawn first), lower depth = on top (drawn last)
        tiles_with_depth = [(t, t.depth) for t in entry.tiles if t.graphic]
        tiles_with_depth.sort(key=lambda x: x[1], reverse=True)  # highest depth first (background)

        result = QPixmap(TILE_SIZE, TILE_SIZE)
        result.fill(Qt.GlobalColor.transparent)
        painter = QPainter(result)

        for tile_info, _ in tiles_with_depth:
            if tile_info.graphic:
                gfx_pm = self.gfx_cache.get_graphic_pixmap(tile_info.graphic)
                ox, oy = tile_info.graphic.handle_pos
                painter.drawPixmap(-ox, -oy, gfx_pm)

        painter.end()

        self._pixmaps[tile_idx] = result
        return result
