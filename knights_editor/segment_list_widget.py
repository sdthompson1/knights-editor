"""Segment list widget showing segment names/thumbnails."""

from PySide6.QtWidgets import QListWidget, QListWidgetItem, QAbstractItemView
from PySide6.QtGui import QDrag, QCursor
from PySide6.QtCore import Signal, Qt


class SegmentListWidget(QListWidget):
    """List of segments. Click to select which segment to edit; drag an
    entry to reorder segments within the file."""

    segment_selected = Signal(int)  # index into segments list
    # A row was dragged to a new position: (old index, new index). The
    # widget has already moved its own item; the owner must reorder its
    # segment list to match.
    segment_moved = Signal(int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.currentRowChanged.connect(self._on_row_changed)
        self._drag_row: int | None = None  # row being dragged, during startDrag
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setDropIndicatorShown(True)

    def set_segments(self, segment_names: list[str]):
        self.clear()
        for name in segment_names:
            item = QListWidgetItem(name)
            # Items are not drop targets themselves: drops always go
            # between rows, so Qt draws a line indicator above/below the
            # hovered row (whichever edge is nearer) rather than a box
            # around it. _drop_row() uses the same nearest-edge rule.
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsDropEnabled)
            self.addItem(item)
        if segment_names:
            self.setCurrentRow(0)

    def _on_row_changed(self, row):
        if row >= 0:
            self.segment_selected.emit(row)

    # --- Drag and drop reordering ---

    def _drop_row(self, pos) -> int:
        """Insertion index (in the current list) for a drop at viewport
        position `pos`: before the hovered row if the pointer is in its
        upper half, after it if in the lower half; the end if on blank
        space."""
        index = self.indexAt(pos)
        if not index.isValid():
            return self.count()
        rect = self.visualRect(index)
        return index.row() if pos.y() < rect.center().y() else index.row() + 1

    def startDrag(self, supported_actions):
        """Run the drag ourselves instead of via QAbstractItemView::startDrag.

        Qt's version deletes the selected row after any drop that reports
        MoveAction (QAbstractItemViewPrivate::clearOrRemove) unless its
        own dropEvent did the move - a private flag we can't set - and in
        InternalMove mode it strips CopyAction from the drag, so the
        "report the drop as a copy" trick doesn't work either. Since
        dropEvent moves the row itself, nothing must happen afterwards.
        """
        indexes = self.selectedIndexes()
        if not indexes:
            return
        row = indexes[0].row()
        drag = QDrag(self)
        # The model's own mime data, so the default dragEnter/dragMove
        # handling (and the drop indicator) accept it as usual.
        drag.setMimeData(self.model().mimeData(indexes))
        rect = self.visualRect(indexes[0])
        drag.setPixmap(self.viewport().grab(rect))
        drag.setHotSpot(self.viewport().mapFromGlobal(QCursor.pos()) - rect.topLeft())
        self._drag_row = row
        try:
            drag.exec(Qt.DropAction.MoveAction)
        finally:
            self._drag_row = None

    def dropEvent(self, event):
        src = self._drag_row
        if src is None or event.source() is not self:
            event.ignore()  # not one of our own drags
            return
        dst = self._drop_row(event.position().toPoint())
        if dst > src:
            dst -= 1  # account for the row being removed before insertion
        event.acceptProposedAction()
        self.move_row(src, dst)

    def move_row(self, src: int, dst: int):
        """Move the item at row `src` so that it ends up at row `dst`,
        keeping it selected, and emit segment_moved. No-op if src == dst."""
        if src == dst or not (0 <= src < self.count()) \
                or not (0 <= dst < self.count()):
            return
        # The selected segment doesn't change, only its row, so suppress
        # the intermediate currentRowChanged signals: they would point at
        # the wrong segment until the owner has reordered its own list.
        self.blockSignals(True)
        try:
            item = self.takeItem(src)
            self.insertItem(dst, item)
            self.setCurrentRow(dst)
        finally:
            self.blockSignals(False)
        self.segment_moved.emit(src, dst)
