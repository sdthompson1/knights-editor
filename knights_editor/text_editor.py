"""Plain text editor widget."""

from PySide6.QtWidgets import (
    QPlainTextEdit, QFrame, QHBoxLayout, QLineEdit, QToolButton,
    QInputDialog, QTextEdit,
)
from PySide6.QtGui import (
    QFont, QTextDocument, QTextCursor, QTextFormat, QPalette,
)
from PySide6.QtCore import Qt, Signal, QEvent

from knights_editor.line_endings import read_text_preserving
from knights_editor.text_files import write_text_atomic
from knights_editor.file_stamp import file_stamp

BOM = '\ufeff'  # UTF-8 byte order mark, as decoded


def _utf16_len(s: str) -> int:
    """Length of `s` in UTF-16 code units, which is what Qt's text
    positions (QTextCursor, QTextBlock.length()) count."""
    return len(s.encode("utf-16-le")) // 2


BASE_FONT_SIZE = 10
MIN_FONT_SIZE = 5
MAX_FONT_SIZE = 40


class FindBar(QFrame):
    """Small find toolbar shown as an overlay in the editor's top-right
    corner. Searches incrementally as you type; Enter / Shift+Enter step
    through matches, Esc closes."""

    def __init__(self, editor: 'TextEditor'):
        super().__init__(editor)
        self._editor = editor
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setAutoFillBackground(True)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 3, 6, 3)
        layout.setSpacing(4)

        self.edit = QLineEdit()
        self.edit.setPlaceholderText("Find")
        self.edit.setMinimumWidth(160)
        self.edit.textChanged.connect(self._on_criteria_changed)
        self.edit.installEventFilter(self)
        layout.addWidget(self.edit)

        self.case_btn = QToolButton()
        self.case_btn.setText("Aa")
        self.case_btn.setCheckable(True)
        self.case_btn.setToolTip("Match case")
        self.case_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.case_btn.toggled.connect(self._on_criteria_changed)
        layout.addWidget(self.case_btn)

        prev_btn = QToolButton()
        prev_btn.setArrowType(Qt.ArrowType.UpArrow)
        prev_btn.setToolTip("Previous match (Shift+Enter)")
        prev_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        prev_btn.clicked.connect(self.find_previous)
        layout.addWidget(prev_btn)

        next_btn = QToolButton()
        next_btn.setArrowType(Qt.ArrowType.DownArrow)
        next_btn.setToolTip("Next match (Enter)")
        next_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        next_btn.clicked.connect(self.find_next)
        layout.addWidget(next_btn)

        close_btn = QToolButton()
        close_btn.setText("✕")
        close_btn.setToolTip("Close (Esc)")
        close_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        close_btn.clicked.connect(self.close_bar)
        layout.addWidget(close_btn)

    def open_bar(self):
        cursor = self._editor.textCursor()
        if cursor.hasSelection():
            sel = cursor.selectedText()
            # U+2029 is Qt's paragraph separator: skip multi-line selections
            if sel and '\u2029' not in sel:
                self.edit.setText(sel)
        self.show()
        self.reposition()
        self.edit.setFocus()
        self.edit.selectAll()

    def close_bar(self):
        self.hide()
        self._set_found(True)
        self._editor.setFocus()

    def reposition(self):
        size = self.sizeHint()
        sb = self._editor.verticalScrollBar()
        sb_width = sb.width() if sb.isVisible() else 0
        x = self._editor.width() - size.width() - sb_width - 4
        self.move(max(0, x), 4)
        self.resize(size)

    def find_next(self):
        self._search()

    def find_previous(self):
        self._search(backwards=True)

    def _on_criteria_changed(self, *_):
        # Re-search from the start of the current selection so that typing
        # more characters extends the current match instead of jumping past
        # it.
        self._search(from_selection_start=True)

    def _search(self, backwards: bool = False,
                from_selection_start: bool = False):
        text = self.edit.text()
        if not text:
            self._set_found(True)
            return

        flags = QTextDocument.FindFlag(0)
        if backwards:
            flags |= QTextDocument.FindFlag.FindBackward
        if self.case_btn.isChecked():
            flags |= QTextDocument.FindFlag.FindCaseSensitively

        if from_selection_start:
            cursor = self._editor.textCursor()
            if cursor.hasSelection():
                cursor.setPosition(cursor.selectionStart())
                self._editor.setTextCursor(cursor)

        found = self._editor.find(text, flags)
        if not found:
            # Wrap around
            original = self._editor.textCursor()
            self._editor.moveCursor(
                QTextCursor.MoveOperation.End if backwards
                else QTextCursor.MoveOperation.Start)
            found = self._editor.find(text, flags)
            if not found:
                self._editor.setTextCursor(original)
        self._set_found(found)

    def _set_found(self, found: bool):
        self.edit.setStyleSheet(
            "" if found else "background-color: #aa3333; color: white;")

    def eventFilter(self, obj, event):
        if obj is self.edit and event.type() == QEvent.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                    self.find_previous()
                else:
                    self.find_next()
                return True
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.close_bar()
            event.accept()
            return
        super().keyPressEvent(event)


class TextEditor(QPlainTextEdit):
    """Simple plain text editor for non-Lua, non-segment files."""

    modified_changed = Signal(bool)

    def __init__(self, filepath: str, parent=None):
        super().__init__(parent)
        self.filepath = filepath
        self._unsaved = False
        # Set by mark_modified: the content differs from disk for a
        # reason other than edits, so undoing back to the document's
        # "clean" state must not make the editor count as saved.
        self._differs_from_disk = False

        font = QFont("Monospace", BASE_FONT_SIZE)
        font.setStyleHint(QFont.StyleHint.TypeWriter)
        self.setFont(font)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(' ') * 4)

        # The original line-ending style (and any UTF-8 byte order mark)
        # is detected and preserved on save.
        self._bom = False
        self._load_text(filepath)
        # On-disk state the editor's content corresponds to (see
        # disk_changed); updated after every read and write.
        self.disk_stamp = file_stamp(filepath)

        self._find_bar = FindBar(self)
        self._find_bar.hide()

        self.document().modificationChanged.connect(self._on_modification_changed)

        # Subtle highlight of the line containing the cursor (hidden while
        # a selection is active, as in VS Code).
        self.cursorPositionChanged.connect(self._update_current_line_highlight)
        self.selectionChanged.connect(self._update_current_line_highlight)
        self._update_current_line_highlight()

    @property
    def unsaved(self) -> bool:
        return self._unsaved

    def _on_modification_changed(self, changed: bool):
        self._set_unsaved(changed or self._differs_from_disk)

    def _set_unsaved(self, value: bool):
        if value != self._unsaved:
            self._unsaved = value
            self.modified_changed.emit(value)

    def _update_current_line_highlight(self):
        selections = []
        if not self.textCursor().hasSelection():
            base = self.palette().color(QPalette.ColorRole.Base)
            tint = (base.lighter(125) if base.lightness() < 128
                    else base.darker(104))
            sel = QTextEdit.ExtraSelection()
            sel.format.setBackground(tint)
            sel.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
            sel.cursor = self.textCursor()
            sel.cursor.clearSelection()
            selections.append(sel)
        self.setExtraSelections(selections)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.PaletteChange:
            self._update_current_line_highlight()

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
        even though nothing was typed (the disk copy changed under us and
        the user chose to keep this version). Only Save or Revert clear
        the flag: there is no edit to undo back to a clean state.

        This can't be done with QTextDocument.setModified(True) alone:
        that is a no-op when the document is already modified, leaving
        its "clean" undo position in place, so undoing back to it would
        report the editor as clean while it still differs from disk."""
        self._differs_from_disk = True
        self._set_unsaved(True)

    def _load_text(self, filepath: str):
        """Read the file into the document, noting its line-ending style
        and whether it starts with a byte order mark (which Qt would drop
        when inserting the text, so it is stripped here and put back by
        save)."""
        text, self._line_ending = read_text_preserving(filepath)
        self._bom = text.startswith(BOM)
        if self._bom:
            text = text[len(BOM):]
        self.setPlainText(text)

    def content_text(self) -> str:
        """The document's text with "\n" line separators, exactly as it
        will be saved (apart from line-ending translation and the BOM).

        toPlainText() is deliberately not used: it rewrites characters
        the user never touched (no-break spaces become plain spaces,
        U+2028 line separators become newlines) which would corrupt e.g.
        string literals on every save. toRawText() keeps them; only the
        block separator Qt uses internally needs translating.
        """
        return self.document().toRawText().replace('\u2029', '\n')

    def save(self):
        text = self.content_text().replace('\n', self._line_ending)
        if self._bom:
            text = BOM + text
        write_text_atomic(self.filepath, text)
        self.disk_stamp = file_stamp(self.filepath)
        self._differs_from_disk = False
        self.document().setModified(False)
        self._set_unsaved(False)

    def revert(self):
        """Discard unsaved changes by re-reading the file from disk (also
        used to pick up external modifications).

        Undo history is cleared (it refers to the discarded text); the
        cursor stays near its old position where possible.
        """
        pos = self.textCursor().position()
        self._load_text(self.filepath)
        self.disk_stamp = file_stamp(self.filepath)
        # Clamp to the new document rather than len(text): the document's
        # character count differs from the string's (the final block's
        # separator, line endings folded to one character, ...).
        cursor = self.textCursor()
        cursor.setPosition(min(pos, self.document().characterCount() - 1))
        self.setTextCursor(cursor)
        self._differs_from_disk = False
        self.document().setModified(False)
        self._set_unsaved(False)

    def show_find_bar(self):
        self._find_bar.open_bar()

    def find_next(self):
        if self._find_bar.isHidden() or not self._find_bar.edit.text():
            self._find_bar.open_bar()
        else:
            self._find_bar.find_next()

    def find_previous(self):
        if self._find_bar.isHidden() or not self._find_bar.edit.text():
            self._find_bar.open_bar()
        else:
            self._find_bar.find_previous()

    def goto_line(self):
        line_count = self.blockCount()
        current = self.textCursor().blockNumber() + 1
        line, ok = QInputDialog.getInt(
            self, "Go to Line", f"Line number (1–{line_count}):",
            current, 1, line_count)
        if not ok:
            return
        self.goto_line_number(line)

    def goto_line_number(self, line: int):
        """Move the cursor to the start of 1-based `line` (clamped to the
        document) and scroll it into view."""
        line = max(1, min(line, self.blockCount()))
        block = self.document().findBlockByNumber(line - 1)
        cursor = QTextCursor(block)
        self.setTextCursor(cursor)
        self.centerCursor()
        self.setFocus()

    def select_range(self, line: int, col: int, length: int):
        """Select `length` characters starting at 0-based `col` on
        1-based `line` (clamped to the document) and scroll them into
        view. Used by Find in Files to show a match.

        `col` and `length` count Python code points (as produced by
        searching a str); Qt cursor positions count UTF-16 code units,
        so they are converted against the block's text (an emoji or any
        other astral character earlier on the line occupies two units).
        """
        line = max(1, min(line, self.blockCount()))
        block = self.document().findBlockByNumber(line - 1)
        text = block.text()
        col = max(0, min(col, len(text)))
        end = max(col, min(col + length, len(text)))
        cursor = QTextCursor(block)
        cursor.setPosition(block.position() + _utf16_len(text[:col]))
        cursor.setPosition(block.position() + _utf16_len(text[:end]),
                           QTextCursor.MoveMode.KeepAnchor)
        self.setTextCursor(cursor)
        self.centerCursor()
        self.setFocus()

    def _set_font_size(self, size: int):
        size = max(MIN_FONT_SIZE, min(MAX_FONT_SIZE, size))
        font = self.font()
        font.setPointSize(size)
        self.setFont(font)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(' ') * 4)

    def zoom_in(self):
        self._set_font_size(self.font().pointSize() + 1)

    def zoom_out(self):
        self._set_font_size(self.font().pointSize() - 1)

    def zoom_reset(self):
        self._set_font_size(BASE_FONT_SIZE)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._find_bar.reposition()

    def keyPressEvent(self, event):
        if (event.key() == Qt.Key.Key_Escape
                and self._find_bar.isVisible()):
            self._find_bar.close_bar()
            event.accept()
            return
        super().keyPressEvent(event)

    def wheelEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            if event.angleDelta().y() > 0:
                self.zoom_in()
            elif event.angleDelta().y() < 0:
                self.zoom_out()
            event.accept()
        else:
            super().wheelEvent(event)
