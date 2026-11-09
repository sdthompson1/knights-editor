"""Module Load Order dialog.

Lets the user choose which installed mods are loaded alongside the mod
being edited, and in what order - the editor's equivalent of the game's
modules.txt. By default the order is inferred (see
mod_loader.discover_load_order); the user can switch to an explicit
order to, for example, test how the mod behaves when an optional
dependency is absent, or to include a mod that patches this one and so
must load after it.
"""

import html

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QCheckBox, QDialogButtonBox, QAbstractItemView,
)
from PySide6.QtCore import Qt

from knights_editor.mod_loader import Discovery, parse_lua_error


class LoadOrderDialog(QDialog):
    """Edit the module load order for one mod.

    After exec() returns Accepted, result_order() gives the chosen
    order: None for automatic inference, otherwise the enabled mod names
    in load order (always including the edited mod).

    Mods are identified throughout by directory name; `declared_names`
    (directory name -> name passed to mod.RegisterMod) is only used to
    label them in the list.
    """

    def __init__(self, parent, mod_name: str, installed: list[str],
                 declared_names: dict[str, str],
                 discovery: Discovery, saved: list[str] | None):
        super().__init__(parent)
        self.setWindowTitle("Module Load Order")
        self._mod_name = mod_name
        self._installed = list(installed)
        self._declared_names = dict(declared_names)
        self._discovery = discovery
        self._result: list[str] | None = None

        layout = QVBoxLayout(self)

        intro = QLabel(
            f"Choose which installed modules are loaded along with "
            f"<b>{mod_name}</b>, and in what order (like the game's "
            f"\"Mods\" dialog). Modules load from top to bottom; require() "
            f"and mod.GetRegisteredMod() only see modules loaded earlier."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        intro2 = QLabel(
            f"If the module you are looking for doesn't appear below, then go "
            f"to File > Settings and add its parent folder to \"Additional "
            f"modules directories\"."
        )
        intro2.setWordWrap(True)
        layout.addWidget(intro2)

        self.auto_check = QCheckBox(
            "Infer the load order automatically from require() and "
            "mod.GetRegisteredMod() / IsModRegistered() calls")
        self.auto_check.setChecked(saved is None)
        self.auto_check.toggled.connect(self._on_auto_toggled)
        layout.addWidget(self.auto_check)

        row = QHBoxLayout()
        self.list = QListWidget()
        self.list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.list.itemChanged.connect(lambda _item: self._refresh())
        self.list.model().rowsMoved.connect(lambda *_a: self._refresh())
        self.list.currentRowChanged.connect(
            lambda _row: self._update_buttons())
        row.addWidget(self.list, 1)

        btn_col = QVBoxLayout()
        self.up_btn = QPushButton("Move &Up")
        self.up_btn.clicked.connect(lambda: self._move(-1))
        self.down_btn = QPushButton("Move &Down")
        self.down_btn.clicked.connect(lambda: self._move(1))
        self.reset_btn = QPushButton("&Reset to Inferred Order")
        self.reset_btn.clicked.connect(self._populate_inferred)
        btn_col.addWidget(self.up_btn)
        btn_col.addWidget(self.down_btn)
        btn_col.addStretch()
        btn_col.addWidget(self.reset_btn)
        row.addLayout(btn_col)
        layout.addLayout(row)

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

        if saved is None:
            self._populate_inferred()
        else:
            self._populate(saved)
        self._on_auto_toggled(self.auto_check.isChecked())
        self.list.setCurrentRow(0)
        self.setMinimumSize(560, 420)

    # --- list contents -------------------------------------------------

    def _label(self, name: str) -> str:
        """How the mod in directory `name` is shown in the list: its
        declared name, then the directory name if that is different."""
        declared = self._declared_names.get(name, name)
        return declared if declared == name else f"{declared} ({name})"

    def _add_item(self, name: str, enabled: bool):
        label = self._label(name)
        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, name)
        flags = (Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled
                 | Qt.ItemFlag.ItemIsDragEnabled)
        if name == self._mod_name:
            # Always loaded; only its position can change.
            item.setText(f"{label}  (this mod)")
            item.setCheckState(Qt.CheckState.Checked)
        elif name not in self._installed:
            item.setText(f"{label}  (not installed)")
            item.setCheckState(Qt.CheckState.Unchecked)
        else:
            item.setText(label)
            flags |= Qt.ItemFlag.ItemIsUserCheckable
            item.setCheckState(Qt.CheckState.Checked if enabled
                               else Qt.CheckState.Unchecked)
        item.setFlags(flags)
        self.list.addItem(item)

    def _populate(self, enabled_order: list[str]):
        """Fill the list: `enabled_order` first (enabled), then every
        other installed mod (disabled). The edited mod is always
        present."""
        self.list.blockSignals(True)
        self.list.clear()
        seen = set()
        for name in enabled_order:
            if name in seen:
                continue
            seen.add(name)
            self._add_item(name, True)
        if self._mod_name not in seen:
            seen.add(self._mod_name)
            self._add_item(self._mod_name, True)
        for name in sorted(self._installed, key=self._label):
            if name not in seen:
                self._add_item(name, False)
        self.list.blockSignals(False)
        self._refresh()

    def _populate_inferred(self):
        self._populate(self._discovery.order)
        self.list.setCurrentRow(0)

    def _names(self) -> list[tuple[str, bool]]:
        """(name, enabled) for every row, top to bottom."""
        out = []
        for i in range(self.list.count()):
            item = self.list.item(i)
            out.append((item.data(Qt.ItemDataRole.UserRole),
                        item.checkState() == Qt.CheckState.Checked))
        return out

    def enabled_order(self) -> list[str]:
        return [n for n, on in self._names() if on]

    # --- editing -------------------------------------------------------

    def _move(self, delta: int):
        row = self.list.currentRow()
        new_row = row + delta
        if row < 0 or new_row < 0 or new_row >= self.list.count():
            return
        item = self.list.takeItem(row)
        self.list.insertItem(new_row, item)
        self.list.setCurrentRow(new_row)
        self._refresh()

    def _on_auto_toggled(self, auto: bool):
        if auto:
            self._populate_inferred()
        self.list.setEnabled(not auto)
        self.reset_btn.setEnabled(not auto)
        self._update_buttons()

    def _update_buttons(self):
        manual = not self.auto_check.isChecked()
        row = self.list.currentRow()
        self.up_btn.setEnabled(manual and row > 0)
        self.down_btn.setEnabled(manual and 0 <= row < self.list.count() - 1)

    def _refresh(self):
        self._update_buttons()
        self.problems_label.setText(self._describe_problems())

    # --- validation ----------------------------------------------------

    def _describe_problems(self) -> str:
        """Advisory notes about the current order, based on the
        dependency edges seen during inference. Not blocking: loading a
        mod without a dependency may be exactly what the user wants to
        test."""
        names = self._names()
        pos = {n: i for i, (n, on) in enumerate(names) if on}
        enabled = set(pos)
        lines = []

        def where(dep: str) -> str:
            if dep in enabled:
                return "is loaded after it"
            if dep in self._installed or dep == self._mod_name:
                return "is disabled"
            return "is not installed"

        for m in pos:
            for dep in self._discovery.required.get(m, []):
                if dep not in enabled or pos[dep] > pos[m]:
                    lines.append(
                        f"<b>{m}</b> requires <b>{dep}</b>, which "
                        f"{where(dep)}: loading will fail.")
            for dep in self._discovery.optional.get(m, []):
                if dep not in enabled or pos[dep] > pos[m]:
                    lines.append(
                        f"<b>{m}</b> optionally uses <b>{dep}</b> "
                        f"(mod.GetRegisteredMod), which {where(dep)}: it "
                        f"will be treated as absent.")
        for dep, exc in self._discovery.failed_optional.items():
            _, _, err = parse_lua_error(exc)
            if len(err) > 160:
                err = err[:160] + "..."
            lines.append(
                f"Optional dependency <b>{dep}</b> failed to load while "
                f"inferring the order (<i>{html.escape(err)}</i>), so it "
                f"was left out; enabling it here will make loading fail.")
        if self._discovery.error is not None:
            # parse_lua_error copes with an empty message (error() with
            # no value) and drops the loader's wrapper-chunk prefixes.
            _, _, err = parse_lua_error(self._discovery.error)
            if len(err) > 160:
                err = err[:160] + "..."
            lines.append(
                f"Note: inferring the load order failed part-way "
                f"(<i>{html.escape(err)}</i>), so the inferred order may "
                f"be incomplete.")
        if not lines:
            return "No problems detected."
        return "<br>".join(lines)

    # --- result --------------------------------------------------------

    def _accept(self):
        if self.auto_check.isChecked():
            self._result = None
        else:
            self._result = self.enabled_order()
        self.accept()

    def result_order(self) -> list[str] | None:
        """None for automatic inference, else the enabled mods in order."""
        return self._result
