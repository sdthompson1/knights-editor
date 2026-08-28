"""Find in Files: search every text file in the mod (or in the modules
in its load order, or in every installed module) and list the matches,
grouped by file, so that e.g. all uses of a Lua variable can be found
and jumped to.

search_files() is the pure search; FindInFilesPanel is the UI that the
main window shows in a dock at the bottom of the window.
"""

import fnmatch
import os
import re
from dataclasses import dataclass, field
from typing import Callable, Iterator

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QToolButton, QComboBox,
    QLabel, QTreeWidget, QTreeWidgetItem, QStyledItemDelegate,
    QStyleOptionViewItem, QStyle, QApplication, QAbstractItemView,
)
from PySide6.QtGui import (
    QTextDocument, QAbstractTextDocumentLayout, QPalette, QFont,
)
from PySide6.QtCore import Qt, Signal, QEvent


# Directories never worth searching.
SKIP_DIRS = {".git", ".svn", ".hg", "__pycache__"}
# Files larger than this are assumed not to be source and are skipped.
MAX_FILE_SIZE = 2 * 1024 * 1024
# Display: a matched line longer than this is trimmed around the match.
MAX_DISPLAY_CHARS = 160
# A search stops once it has found this many matches: nobody reads more,
# and a tree of millions of items (e.g. from a regex like "." over every
# installed module) would take a very long time to build.
MAX_MATCHES = 5000


@dataclass
class Match:
    path: str
    line: int        # 1-based
    col: int         # 0-based character offset within the line
    length: int      # in characters
    text: str        # the full line (no line ending)


@dataclass
class FileMatches:
    path: str
    relpath: str
    matches: list[Match] = field(default_factory=list)


class SearchError(ValueError):
    """The query (as a regex) is malformed."""


def build_pattern(query: str, case_sensitive: bool, whole_word: bool,
                  regex: bool) -> re.Pattern:
    """Compile the search query to a regular expression.

    Raises SearchError for an invalid regex, or one that obviously
    matches the empty string (e.g. "x*"), which would match everywhere.
    This check is only a friendly early error: a zero-width pattern that
    needs context (r"\b", a lone lookaround) passes it, so search_text()
    also drops any zero-length match it finds.
    """
    source = query if regex else re.escape(query)
    if whole_word:
        # Lookarounds rather than \b so that a query starting or ending
        # with a non-word character (e.g. "kts.Foo") still works.
        source = rf"(?<!\w)(?:{source})(?!\w)"
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        pattern = re.compile(source, flags)
    except re.error as e:
        raise SearchError(str(e)) from e
    if pattern.match("") is not None:
        raise SearchError("pattern matches the empty string")
    return pattern


def parse_file_filter(filter_text: str) -> list[str]:
    """Split "*.lua, *.txt" (commas and/or spaces) into glob patterns.
    An empty result means "all files"."""
    return [p for p in re.split(r"[,\s;]+", filter_text.strip()) if p]


def _matches_filter(name: str, globs: list[str]) -> bool:
    if not globs:
        return True
    return any(fnmatch.fnmatch(name, g) for g in globs)


def _read_text(path: str) -> str | None:
    """The file's text, or None if it isn't (UTF-8) text or is too big."""
    try:
        if os.path.getsize(path) > MAX_FILE_SIZE:
            return None
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    if b"\0" in raw:
        return None
    try:
        # utf-8-sig drops a leading byte order mark, as the editor does
        # when loading the file, so line-1 columns line up with it.
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


def iter_files(root: str, globs: list[str]):
    """Yield the paths under `root` that pass the file filter, in a
    stable (sorted) order."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            if _matches_filter(name, globs):
                yield os.path.join(dirpath, name)


# Exactly the characters QTextCursor.insertText() turns into block
# separators, so that reported line numbers agree with the editor's.
# str.splitlines() must not be used: it also splits on \f, \v, \x1c-\x1e,
# \x85 and U+2028, which Qt keeps inside a block.
_BLOCK_SEPARATOR = re.compile(r"\r\n|[\n\r\u2029\ufdd0\ufdd1]")


def split_lines_like_qt(text: str) -> list[str]:
    """Split `text` into lines exactly as QPlainTextEdit splits it into
    blocks (a trailing newline yields a final empty line, as in Qt)."""
    return _BLOCK_SEPARATOR.split(text)


def search_text(path: str, text: str, pattern: re.Pattern,
                limit: int | None = None) -> list[Match]:
    """The matches of `pattern` in `text` (LF or CRLF line endings), at
    most `limit` of them (all of them if `limit` is None).

    Zero-length matches are ignored: there is nothing to highlight or
    select, and a pattern like r"\b" would otherwise "match" at nearly
    every character.

    Lines are numbered the way the editor numbers its blocks, and
    columns are code-point offsets into the line (the editor converts
    them to Qt's UTF-16 positions, see TextEditor.select_range)."""
    result = []
    if limit is not None and limit <= 0:
        return result
    for lineno, line in enumerate(split_lines_like_qt(text), start=1):
        for m in pattern.finditer(line):
            if m.end() == m.start():
                continue
            result.append(Match(path, lineno, m.start(),
                                m.end() - m.start(), line))
            if limit is not None and len(result) >= limit:
                return result
    return result


def search_files(root: str, pattern: re.Pattern, globs: list[str],
                 text_provider: Callable[[str], str | None] | None = None,
                 limit: int | None = None) -> Iterator[FileMatches]:
    """Search every text file under `root` whose name matches `globs`,
    yielding a FileMatches for each file that has any, in iter_files()
    order. Stops after `limit` matches in total (unlimited if None).

    `text_provider(path)` may return the current (possibly unsaved)
    editor text for a file, so results reflect what's on screen rather
    than the stale copy on disk; it returns None for files not open.
    """
    remaining = limit
    for path in iter_files(root, globs):
        if remaining is not None and remaining <= 0:
            return
        text = text_provider(path) if text_provider is not None else None
        if text is None:
            text = _read_text(path)
            if text is None:
                continue
        matches = search_text(path, text, pattern, remaining)
        if matches:
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            yield FileMatches(path, rel, matches)
            if remaining is not None:
                remaining -= len(matches)


# ---------------------------------------------------------------------------
# UI

# Item data roles
LOCATION_ROLE = Qt.ItemDataRole.UserRole          # (path, line, col, length)
MATCH_ROLE = Qt.ItemDataRole.UserRole + 1         # the Match, for the delegate


class _MatchDelegate(QStyledItemDelegate):
    """Draws result rows as rich text so the matched text can be
    highlighted; rows without MATCH_ROLE data draw normally."""

    def paint(self, painter, option, index):
        match = index.data(MATCH_ROLE)
        if match is None:
            super().paint(painter, option, index)
            return
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        widget = opt.widget
        style = widget.style() if widget is not None else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt,
                          painter, widget)

        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        group = (QPalette.ColorGroup.Active
                 if opt.state & QStyle.StateFlag.State_Active
                 else QPalette.ColorGroup.Inactive)
        text_color = opt.palette.color(
            group, QPalette.ColorRole.HighlightedText if selected
            else QPalette.ColorRole.Text)
        base = opt.palette.color(group, QPalette.ColorRole.Base)
        highlight_bg = "#6a5a1a" if base.lightness() < 128 else "#ffe680"
        highlight_fg = "#ffffff" if base.lightness() < 128 else "#000000"
        # The line number is de-emphasised, except on a selected row
        # where a grey would be unreadable against the highlight colour.
        number_color = text_color.name() if selected else "gray"

        doc = QTextDocument()
        doc.setDefaultFont(opt.font)
        doc.setDocumentMargin(0)
        doc.setHtml(format_match_html(match, highlight_bg, highlight_fg,
                                      number_color))
        rect = style.subElementRect(
            QStyle.SubElement.SE_ItemViewItemText, opt, widget)
        ctx = QAbstractTextDocumentLayout.PaintContext()
        ctx.palette.setColor(QPalette.ColorRole.Text, text_color)
        painter.save()
        painter.setClipRect(rect)
        painter.translate(rect.left(),
                          rect.top() + (rect.height() - doc.size().height()) / 2)
        doc.documentLayout().draw(painter, ctx)
        painter.restore()


def _html_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace(" ", "&nbsp;"))


def format_match_html(m: Match, highlight_bg: str, highlight_fg: str,
                      number_color: str) -> str:
    """Rich text for one result row: "  12:  text with <match> in it",
    trimmed around the match if the line is very long."""
    text = m.text
    start, end = m.col, m.col + m.length
    # Drop leading indentation (it carries no information here).
    stripped = text.lstrip()
    lead = len(text) - len(stripped)
    text, start, end = stripped, max(0, start - lead), max(0, end - lead)
    prefix, suffix = "", ""
    if len(text) > MAX_DISPLAY_CHARS:
        lo = max(0, start - MAX_DISPLAY_CHARS // 3)
        hi = min(len(text), lo + MAX_DISPLAY_CHARS)
        if lo > 0:
            prefix = "…"
        if hi < len(text):
            suffix = "…"
        text = text[lo:hi]
        start, end = start - lo, min(end - lo, len(text))
    return (f'<span style="color: {number_color};">{m.line}:</span>'
            f'&nbsp;&nbsp;{prefix}{_html_escape(text[:start])}'
            f'<span style="background-color: {highlight_bg}; '
            f'color: {highlight_fg}; font-weight: bold;">'
            f'{_html_escape(text[start:end])}</span>'
            f'{_html_escape(text[end:])}{suffix}')


class FindInFilesPanel(QWidget):
    """Search bar plus results tree.

    Emits open_location(path, line, col, length) when the user activates
    a result. set_roots() tells the panel where to search; the
    text_provider passed to the constructor supplies unsaved editor
    buffers (see search_files).
    """

    open_location = Signal(str, int, int, int)
    close_requested = Signal()

    SCOPE_MOD = 0      # the open mod only
    SCOPE_LOADED = 1   # the mods in the current load order
    SCOPE_ALL = 2      # every mod in every modules directory

    def __init__(self, text_provider=None, parent=None):
        super().__init__(parent)
        self._text_provider = text_provider
        self._mod_dir: str | None = None
        # (mod name, directory) pairs for the two wider scopes
        self._loaded_mods: list[tuple[str, str]] = []
        self._all_mods: list[tuple[str, str]] = []
        self._results: list[FileMatches] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(3)

        bar = QHBoxLayout()
        bar.setSpacing(4)

        self.query = QLineEdit()
        self.query.setPlaceholderText("Find in files (Enter to search)")
        self.query.setClearButtonEnabled(True)
        self.query.returnPressed.connect(self.run_search)
        self.query.installEventFilter(self)
        bar.addWidget(self.query, 3)

        self.case_btn = self._option_button("Aa", "Match case")
        bar.addWidget(self.case_btn)
        self.word_btn = self._option_button("ab", "Whole word")
        bar.addWidget(self.word_btn)
        self.regex_btn = self._option_button(".*", "Regular expression")
        bar.addWidget(self.regex_btn)

        bar.addSpacing(8)
        bar.addWidget(QLabel("Files:"))
        self.filter_combo = QComboBox()
        self.filter_combo.setEditable(True)
        self.filter_combo.addItems(["*.lua", "*.lua, *.txt", "*.txt", "*"])
        self.filter_combo.setToolTip(
            "File name patterns to search, separated by commas "
            "(* matches all files)")
        self.filter_combo.lineEdit().returnPressed.connect(self.run_search)
        self.filter_combo.setMinimumContentsLength(10)
        bar.addWidget(self.filter_combo, 1)

        bar.addWidget(QLabel("In:"))
        self.scope_combo = QComboBox()
        self.scope_combo.addItems(
            ["This mod", "All loaded modules", "All modules"])
        self.scope_combo.setToolTip(
            "Where to search: only the open mod; the modules in its load "
            "order (this mod, base, dependencies, ...); or every module in "
            "knights_data/modules and the additional modules directories")
        bar.addWidget(self.scope_combo)

        self.search_btn = QToolButton()
        self.search_btn.setText("Search")
        self.search_btn.clicked.connect(self.run_search)
        bar.addWidget(self.search_btn)

        close_btn = QToolButton()
        close_btn.setText("✕")
        close_btn.setToolTip("Close (Esc)")
        close_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        close_btn.clicked.connect(self.close_requested)
        bar.addWidget(close_btn)
        layout.addLayout(bar)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setItemDelegate(_MatchDelegate(self.tree))
        self.tree.itemActivated.connect(self._on_item_activated)
        self.tree.installEventFilter(self)
        mono = QFont("Monospace")
        mono.setStyleHint(QFont.StyleHint.TypeWriter)
        self._mono = mono
        layout.addWidget(self.tree, 1)

        self.status = QLabel("")
        layout.addWidget(self.status)

    @staticmethod
    def _option_button(text: str, tip: str) -> QToolButton:
        btn = QToolButton()
        btn.setText(text)
        btn.setCheckable(True)
        btn.setToolTip(tip)
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        return btn

    # -- configuration -----------------------------------------------------

    def set_roots(self, mod_dir: str | None,
                  loaded_mods: list[tuple[str, str]],
                  all_mods: list[tuple[str, str]]):
        """Where the three scopes search: the open mod's directory, the
        (name, directory) of each mod in the current load order, and of
        every installed mod. Results outside "This mod" are labelled
        with the mod name."""
        self._mod_dir = mod_dir
        self._loaded_mods = list(loaded_mods)
        self._all_mods = list(all_mods)
        model = self.scope_combo.model()
        for scope in (self.SCOPE_MOD, self.SCOPE_LOADED, self.SCOPE_ALL):
            model.item(scope).setEnabled(bool(self._scope_roots(scope)))
        if not self._scope_roots(self.scope_combo.currentIndex()):
            self.scope_combo.setCurrentIndex(self.SCOPE_MOD)

    def _scope_roots(self, scope: int) -> list[tuple[str, str]]:
        """(label, directory) roots for a scope; the label prefixes the
        displayed file paths and is empty for the single-mod scope."""
        if scope == self.SCOPE_LOADED:
            return self._loaded_mods
        if scope == self.SCOPE_ALL:
            return self._all_mods
        return [("", self._mod_dir)] if self._mod_dir else []

    def _current_roots(self) -> list[tuple[str, str]]:
        return self._scope_roots(self.scope_combo.currentIndex())

    def focus_query(self, initial_text: str | None = None):
        if initial_text:
            self.query.setText(initial_text)
        self.query.setFocus()
        self.query.selectAll()

    # -- searching ---------------------------------------------------------

    def run_search(self):
        text = self.query.text()
        roots = self._current_roots()
        self.tree.clear()
        self._results = []
        if not text:
            self.status.setText("")
            return
        if not roots:
            self.status.setText("Open a mod first (File > Open Mod).")
            return
        try:
            pattern = build_pattern(
                text, self.case_btn.isChecked(), self.word_btn.isChecked(),
                self.regex_btn.isChecked())
        except SearchError as e:
            self.status.setText(f"Invalid pattern: {e}")
            return
        globs = parse_file_filter(self.filter_combo.currentText())
        seen_dirs = set()
        total = 0
        for label, root in roots:
            if total >= MAX_MATCHES:
                break
            # The same directory can appear under two names (e.g. a mod
            # edited in place and also installed); search it once.
            key = os.path.realpath(root)
            if key in seen_dirs or not os.path.isdir(root):
                continue
            seen_dirs.add(key)
            for fm in search_files(root, pattern, globs, self._text_provider,
                                   MAX_MATCHES - total):
                if label:
                    fm.relpath = f"{label}/{fm.relpath}"
                self._results.append(fm)
                total += len(fm.matches)
        self._populate()

    def _populate(self):
        total = sum(len(f.matches) for f in self._results)
        for fm in self._results:
            top = QTreeWidgetItem(self.tree)
            top.setText(0, f"{fm.relpath}  ({len(fm.matches)})")
            top.setFlags(top.flags() & ~Qt.ItemFlag.ItemIsSelectable
                         | Qt.ItemFlag.ItemIsEnabled)
            for m in fm.matches:
                item = QTreeWidgetItem(top)
                item.setFont(0, self._mono)
                # Plain text fallback (tooltips, accessibility, tests)
                item.setText(0, f"{m.line}: {m.text.strip()}")
                item.setToolTip(0, f"{fm.relpath}:{m.line}")
                item.setData(0, MATCH_ROLE, m)
                item.setData(0, LOCATION_ROLE,
                             (m.path, m.line, m.col, m.length))
            top.setExpanded(True)
        files = len(self._results)
        if total == 0:
            self.status.setText("No matches.")
        elif total >= MAX_MATCHES:
            # The search stopped at the cap, so there may be more.
            self.status.setText(
                f"Stopped after {total} matches in {files} "
                f"file{'s' if files != 1 else ''} — "
                "refine the search to see the rest.")
        else:
            self.status.setText(
                f"{total} match{'es' if total != 1 else ''} in "
                f"{files} file{'s' if files != 1 else ''}")
        first = self._first_result_item()
        if first is not None:
            self.tree.setCurrentItem(first)

    # -- navigation --------------------------------------------------------

    def _result_items(self) -> list[QTreeWidgetItem]:
        items = []
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            for j in range(top.childCount()):
                items.append(top.child(j))
        return items

    def _first_result_item(self):
        items = self._result_items()
        return items[0] if items else None

    def has_results(self) -> bool:
        return bool(self._result_items())

    def _on_item_activated(self, item, _column=0):
        loc = item.data(0, LOCATION_ROLE)
        if loc is None:
            item.setExpanded(not item.isExpanded())
            return
        self.open_location.emit(*loc)

    def goto_adjacent(self, delta: int) -> bool:
        """Select and open the next (delta=1) or previous (delta=-1)
        result relative to the current one, wrapping around. Returns
        False if there are no results."""
        items = self._result_items()
        if not items:
            return False
        current = self.tree.currentItem()
        if current is None or current.data(0, LOCATION_ROLE) is None:
            index = 0 if delta > 0 else len(items) - 1
        else:
            index = (items.index(current) + delta) % len(items)
        item = items[index]
        self.tree.setCurrentItem(item)
        self.tree.scrollToItem(item)
        self.open_location.emit(*item.data(0, LOCATION_ROLE))
        return True

    # -- keyboard ----------------------------------------------------------

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key == Qt.Key.Key_Escape:
                self.close_requested.emit()
                return True
            if obj is self.query and key == Qt.Key.Key_Down \
                    and self.has_results():
                # Down arrow from the query moves into the results.
                self.tree.setFocus()
                if self.tree.currentItem() is None:
                    self.tree.setCurrentItem(self._first_result_item())
                return True
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.close_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)
