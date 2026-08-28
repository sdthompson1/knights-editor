"""Line-ending detection shared by the text and segment editors, so that
saving a file preserves whichever style (CRLF or LF) it already used.

Freshly created files use the platform default (CRLF on Windows, LF
elsewhere). A file with mixed endings is normalised to its majority style
on save. Qt also treats a lone CR and U+2029 (paragraph separator) as
line breaks when the text is loaded, so the text editor saves those as
ordinary line endings too; everything else, including no-break spaces
and U+2028, is preserved as-is (see TextEditor.content_text)."""

import os

from knights_editor.text_files import read_bytes_as_text

LF = "\n"
CRLF = "\r\n"
PLATFORM_DEFAULT = os.linesep


def detect_line_ending(raw: str) -> str:
    """Return the majority line-ending style of the text. An empty file
    (or an exact tie) gets the platform default."""
    crlf_count = raw.count(CRLF)
    lf_count = raw.count(LF) - crlf_count
    if crlf_count > lf_count:
        return CRLF
    if lf_count > crlf_count:
        return LF
    return PLATFORM_DEFAULT


def read_text_preserving(filepath: str) -> tuple[str, str]:
    """Read a UTF-8 file and return (text with normalised LF endings, its
    detected line-ending style). Raises text_files.NotUtf8Error if the
    file is not valid UTF-8."""
    raw = read_bytes_as_text(filepath)
    return raw.replace(CRLF, LF), detect_line_ending(raw)
