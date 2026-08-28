"""Simple Lua syntax-highlighted text editor widget."""

import re

from PySide6.QtGui import (
    QSyntaxHighlighter, QTextCharFormat, QColor, QFont,
)
from PySide6.QtCore import QRegularExpression

from knights_editor.text_editor import TextEditor


class LuaHighlighter(QSyntaxHighlighter):
    """Basic Lua syntax highlighter."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rules = []

        # Keywords
        kw_fmt = QTextCharFormat()
        kw_fmt.setForeground(QColor(200, 120, 50))
        kw_fmt.setFontWeight(QFont.Weight.Bold)
        keywords = [
            "and", "break", "do", "else", "elseif", "end", "false",
            "for", "function", "if", "in", "local", "nil", "not",
            "or", "repeat", "return", "then", "true", "until", "while",
        ]
        for kw in keywords:
            self._rules.append((QRegularExpression(rf"\b{kw}\b"), kw_fmt))

        # Strings (double and single quoted)
        str_fmt = QTextCharFormat()
        str_fmt.setForeground(QColor(120, 180, 120))
        self._rules.append((QRegularExpression(r'"[^"\\]*(\\.[^"\\]*)*"'), str_fmt))
        self._rules.append((QRegularExpression(r"'[^'\\]*(\\.[^'\\]*)*'"), str_fmt))

        # Numbers
        num_fmt = QTextCharFormat()
        num_fmt.setForeground(QColor(180, 140, 200))
        self._rules.append((QRegularExpression(r"\b\d+\.?\d*\b"), num_fmt))

        # Comments (single line)
        comment_fmt = QTextCharFormat()
        comment_fmt.setForeground(QColor(128, 128, 128))
        comment_fmt.setFontItalic(True)
        self._rules.append((QRegularExpression(r"--[^\n]*"), comment_fmt))

        # kts.* and mod.* calls
        api_fmt = QTextCharFormat()
        api_fmt.setForeground(QColor(100, 160, 220))
        self._rules.append((QRegularExpression(r"\bkts\.\w+"), api_fmt))
        self._rules.append((QRegularExpression(r"\bmod\.\w+"), api_fmt))

        # Long-bracket strings [[...]] / [==[...]==] and block comments
        # --[[...]] may span lines; see _long_brackets.
        self._long_fmts = (str_fmt, comment_fmt)

    # Scanner for the start of a long bracket (optionally a comment), a
    # plain line comment, or a quoted string (to be skipped over: a "[["
    # inside one doesn't open anything).
    _LONG_SCAN = re.compile(
        r"(--)?\[(=*)\[|--|\"(?:[^\"\\]|\\.)*\"?|'(?:[^'\\]|\\.)*'?")

    def _long_brackets(self, text: str) -> list[tuple[int, int, int]]:
        """Find the long-bracket strings/comments in a block: a list of
        (start, end, is_comment) spans in `text`, carrying an unclosed one
        over to the next block via the block state.

        Block state: 0/-1 outside any long bracket, else
        1 + 2 * level + is_comment, where level is the number of '=' in
        the opening bracket (the closer must match it)."""
        spans = []
        pos = 0
        state = self.previousBlockState()
        if state > 0:
            level, is_comment = divmod(state - 1, 2)
            start = 0
        else:
            start = None
        while True:
            if start is not None:
                close = text.find("]" + "=" * level + "]", pos)
                if close < 0:
                    spans.append((start, len(text), is_comment))
                    self.setCurrentBlockState(1 + 2 * level + is_comment)
                    return spans
                end = close + level + 2
                spans.append((start, end, is_comment))
                pos = end
                start = None
            m = self._LONG_SCAN.search(text, pos)
            if m is None:
                break
            if m.group(2) is not None:      # long bracket opener
                level = len(m.group(2))
                is_comment = 1 if m.group(1) else 0
                start = m.start()
            elif m.group(0).startswith("--"):  # line comment: rest is dead
                break
            pos = m.end()                    # (quoted string: skip it)
        self.setCurrentBlockState(0)
        return spans

    def highlightBlock(self, text):
        spans = self._long_brackets(text)
        for pattern, fmt in self._rules:
            it = pattern.globalMatch(text)
            while it.hasNext():
                match = it.next()
                # A rule match starting inside a long bracket (e.g. the
                # line-comment rule on "--[[") must not run past its end.
                if any(a <= match.capturedStart() < b for a, b, _ in spans):
                    continue
                self.setFormat(match.capturedStart(), match.capturedLength(), fmt)
        for a, b, is_comment in spans:
            self.setFormat(a, b - a, self._long_fmts[is_comment])


class LuaEditor(TextEditor):
    """Text editor for Lua files with syntax highlighting.

    Inherits loading/saving, modified tracking, and font-size zoom
    from TextEditor.
    """

    def __init__(self, filepath: str, parent=None):
        super().__init__(filepath, parent)
        self._highlighter = LuaHighlighter(self.document())
