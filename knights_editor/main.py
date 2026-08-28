#!/usr/bin/env python3
"""Knights Mod Editor - main entry point."""

import sys
from PySide6.QtWidgets import QApplication
from knights_editor.main_window import MainWindow, ModalDialogGrabGuard
from knights_editor import config


def main():
    app = QApplication(sys.argv)
    # Guard against a stale X11 pointer grab freezing the display when a
    # modal dialog opens (see main_window.release_stale_mouse_grab).
    app.installEventFilter(ModalDialogGrabGuard(app))

    window = MainWindow()
    window.showMaximized()

    # First-run setup: ask for the knights_data directory if not configured.
    # If the user cancels, don't go on to open a command-line mod: that
    # would immediately re-prompt for the same thing.
    configured = True
    if config.get_knights_data_dir() is None:
        configured = window.prompt_knights_data_dir() is not None

    # If a mod directory was passed on command line, open it
    if configured and len(sys.argv) > 1:
        window.open_mod(sys.argv[1])

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
