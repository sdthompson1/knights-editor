"""Settings dialog (File > Settings).

Edits the editor-wide preferences from config.py:

- the knights_data directory (needed to load mods and their
  dependencies, and passed to the game as -d);
- the Knights executable used by Test Segment;
- the additional modules directories: an ordered list of directories
  that, like knights_data/modules, contain mod subfolders. When a mod is
  looked up by name (require, mod.GetRegisteredMod, the module load
  order) the directory containing the mod being edited is searched
  first, then these in order, then knights_data/modules (see
  mod_loader.modules_search_path); Test Segment passes the directory
  each mod was found in to the game as a --module-dir option, so the
  game loads the same mods. So these only need configuring when
  dependencies live somewhere other than alongside the mod being
  edited.
"""

import os

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QLineEdit,
    QListWidget, QPushButton, QDialogButtonBox, QAbstractItemView,
    QFileDialog, QMessageBox, QGroupBox,
)
from PySide6.QtCore import Qt

from knights_editor import config


class SettingsDialog(QDialog):
    """Edit the editor's settings. Nothing is written to config by the
    dialog itself: after exec() returns Accepted, read the chosen values
    with knights_data_dir(), knights_binary() and extra_modules_dirs()."""

    def __init__(self, parent, knights_data_dir: str | None,
                 knights_binary: str, extra_modules_dirs: list[str],
                 message: str | None = None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        layout = QVBoxLayout(self)

        if message:
            # Why the dialog opened, when it wasn't from the menu (e.g.
            # first run, or a setting turned out to be unusable).
            note = QLabel(message)
            note.setWordWrap(True)
            note.setStyleSheet("font-weight: bold;")
            layout.addWidget(note)

        # --- Paths ---------------------------------------------------
        paths = QGroupBox("Knights")
        grid = QGridLayout(paths)

        data_label = QLabel("Knights &data directory:")
        self.data_dir_edit = QLineEdit(knights_data_dir or "")
        self.data_dir_edit.setPlaceholderText(
            "The folder containing the 'modules' and 'client' subfolders")
        self.data_dir_edit.textChanged.connect(lambda _t: self._refresh())
        data_label.setBuddy(self.data_dir_edit)
        grid.addWidget(data_label, 0, 0)
        grid.addWidget(self.data_dir_edit, 0, 1)
        browse_data = QPushButton("Browse...")
        browse_data.clicked.connect(self._browse_data_dir)
        grid.addWidget(browse_data, 0, 2)

        binary_label = QLabel("Knights &executable:")
        self.binary_edit = QLineEdit(knights_binary)
        self.binary_edit.setPlaceholderText("knights")
        self.binary_edit.textChanged.connect(lambda _t: self._refresh())
        binary_label.setBuddy(self.binary_edit)
        grid.addWidget(binary_label, 1, 0)
        grid.addWidget(self.binary_edit, 1, 1)
        browse_binary = QPushButton("Browse...")
        browse_binary.clicked.connect(self._browse_binary)
        grid.addWidget(browse_binary, 1, 2)

        hint = QLabel(
            "The executable is used by Segment > Test Segment.")
        hint.setWordWrap(True)
        grid.addWidget(hint, 2, 1, 1, 2)
        grid.setColumnStretch(1, 1)
        layout.addWidget(paths)

        # --- Additional modules directories ---------------------------
        extras = QGroupBox("Additional modules directories")
        extras_layout = QVBoxLayout(extras)
        intro = QLabel(
            "Extra directories to search for Lua modules. Note that the parent directory "
            "of the currently loaded module, as well as knights_data/modules, are added "
            "automatically, so don't need to be listed below.")
        intro.setWordWrap(True)
        extras_layout.addWidget(intro)

        row = QHBoxLayout()
        self.list = QListWidget()
        self.list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.list.model().rowsMoved.connect(lambda *_a: self._refresh())
        self.list.currentRowChanged.connect(lambda _row: self._update_buttons())
        for d in extra_modules_dirs:
            self.list.addItem(d)
        row.addWidget(self.list, 1)

        btn_col = QVBoxLayout()
        self.add_btn = QPushButton("&Add...")
        self.add_btn.clicked.connect(self._add_dir)
        self.remove_btn = QPushButton("&Remove")
        self.remove_btn.clicked.connect(self._remove_dir)
        self.up_btn = QPushButton("Move &Up")
        self.up_btn.clicked.connect(lambda: self._move(-1))
        self.down_btn = QPushButton("Move Do&wn")
        self.down_btn.clicked.connect(lambda: self._move(1))
        for b in (self.add_btn, self.remove_btn, self.up_btn, self.down_btn):
            btn_col.addWidget(b)
        btn_col.addStretch()
        row.addLayout(btn_col)
        extras_layout.addLayout(row)
        layout.addWidget(extras)

        # --- Problems + buttons ---------------------------------------
        self.problems_label = QLabel("")
        self.problems_label.setWordWrap(True)
        self.problems_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.problems_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._refresh()
        self.list.setCurrentRow(0)
        self.setMinimumSize(640, 480)

    # --- results -------------------------------------------------------

    def knights_data_dir(self) -> str:
        return self.data_dir_edit.text().strip()

    def knights_binary(self) -> str:
        """The executable; "knights" if the field was left empty."""
        return self.binary_edit.text().strip() or "knights"

    def extra_modules_dirs(self) -> list[str]:
        return [self.list.item(i).text() for i in range(self.list.count())]

    # --- browsing ------------------------------------------------------

    def _browse_data_dir(self):
        current = self.knights_data_dir()
        start = current if os.path.isdir(current) else os.path.expanduser("~")
        path = QFileDialog.getExistingDirectory(
            self, "Select knights_data Directory", start,
            QFileDialog.Option.ShowDirsOnly)
        if path:
            self.data_dir_edit.setText(path)

    def _browse_binary(self):
        current = self.knights_binary()
        start = os.path.dirname(current) if os.path.isabs(current) else ""
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Knights Executable", start)
        if path:
            self.binary_edit.setText(path)

    def _add_dir(self):
        row = self.list.currentRow()
        start = self.list.item(row).text() if row >= 0 else self.knights_data_dir()
        if not os.path.isdir(start):
            start = os.path.expanduser("~")
        path = QFileDialog.getExistingDirectory(
            self, "Select Modules Directory", start,
            QFileDialog.Option.ShowDirsOnly)
        if not path:
            return
        # Compare real paths (as mod_loader.modules_search_path does when
        # de-duplicating) so that a trailing slash, a symlink, or ".."
        # components don't let the same directory be listed twice.
        key = os.path.normcase(os.path.realpath(path))
        for existing in self.extra_modules_dirs():
            if os.path.normcase(os.path.realpath(existing)) == key:
                QMessageBox.information(
                    self, "Already listed",
                    f"{path} is already in the list"
                    + (f" (as {existing})." if existing != path else "."))
                return
        self.list.addItem(path)
        self.list.setCurrentRow(self.list.count() - 1)
        self._refresh()

    def _remove_dir(self):
        row = self.list.currentRow()
        if row < 0:
            return
        self.list.takeItem(row)
        self.list.setCurrentRow(min(row, self.list.count() - 1))
        self._refresh()

    def _move(self, delta: int):
        row = self.list.currentRow()
        new_row = row + delta
        if row < 0 or new_row < 0 or new_row >= self.list.count():
            return
        item = self.list.takeItem(row)
        self.list.insertItem(new_row, item)
        self.list.setCurrentRow(new_row)
        self._refresh()

    def _update_buttons(self):
        row = self.list.currentRow()
        self.remove_btn.setEnabled(row >= 0)
        self.up_btn.setEnabled(row > 0)
        self.down_btn.setEnabled(0 <= row < self.list.count() - 1)

    # --- validation ----------------------------------------------------

    def _refresh(self):
        self._update_buttons()
        self.problems_label.setText(self._describe_problems())

    def _describe_problems(self) -> str:
        """Advisory notes about the current values. Only a missing data
        directory blocks OK (see _accept); the rest are warnings, since
        the user may be setting things up ahead of time."""
        lines = []
        data_dir = self.knights_data_dir()
        if not data_dir:
            lines.append("The Knights data directory is not set; the editor "
                         "cannot load mods without it.")
        elif not os.path.isdir(data_dir):
            lines.append(f"<b>{data_dir}</b> does not exist.")
        elif not os.path.isdir(os.path.join(data_dir, "modules")):
            lines.append(f"<b>{data_dir}</b> has no 'modules' subfolder, so "
                         "it doesn't look like a knights_data directory.")
        binary = self.knights_binary()
        if not config.knights_binary_found(binary):
            lines.append(f"The Knights executable <b>{binary}</b> was not "
                         "found; Test Segment will not work until it is.")
        for d in self.extra_modules_dirs():
            if not os.path.isdir(d):
                lines.append(f"Modules directory <b>{d}</b> does not exist.")
        if not lines:
            return "No problems detected."
        return "<br>".join(lines)

    def _accept(self):
        data_dir = self.knights_data_dir()
        if not data_dir:
            QMessageBox.warning(
                self, "Knights Data Directory",
                "Please set the Knights data directory (the folder that "
                "contains the 'modules' subfolder).")
            self.data_dir_edit.setFocus()
            return
        if not os.path.isdir(os.path.join(data_dir, "modules")):
            reply = QMessageBox.warning(
                self, "No modules folder",
                f"{data_dir} does not contain a 'modules' subfolder, so it "
                "doesn't look like a knights_data directory.\n\n"
                "Use it anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                self.data_dir_edit.setFocus()
                return
        self.accept()
