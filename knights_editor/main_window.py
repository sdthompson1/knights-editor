"""Main window for the Knights Mod Editor."""

import os
import shutil
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QSplitter,
    QFileDialog, QMessageBox, QTabWidget, QTreeView, QLabel,
    QFileSystemModel, QMenu, QInputDialog, QDialog, QPlainTextEdit,
    QPushButton, QApplication, QDockWidget,
)
from PySide6.QtGui import (QAction, QKeySequence, QGuiApplication, QFont,
                           QDesktopServices)
from PySide6.QtCore import Qt, QModelIndex, QTimer, QObject, QEvent, QUrl

from knights_editor.models import TileEntry, Segment
from knights_editor.segment_editor import SegmentEditor, default_fill_tile
from knights_editor.lua_editor import LuaEditor
from knights_editor.text_editor import TextEditor
from knights_editor.segment_io import (load_segments, save_segments, SegmentFormatError,
                        NotSegmentFileError)
from knights_editor.text_files import NotUtf8Error
from knights_editor.graphics_cache import GraphicsCache
from knights_editor.mod_loader import (ModLoader, parse_lua_error, discover_load_order,
                        list_installed_modules, modules_search_path,
                        find_module_dir)
from knights_editor.lua_state_widget import LuaStateWidget
from knights_editor.load_order_dialog import LoadOrderDialog
from knights_editor.settings_dialog import SettingsDialog
from knights_editor.about_dialog import AboutDialog, LUA_DOCS_URL
from knights_editor.find_in_files import FindInFilesPanel
from knights_editor.segtest import (SegtestParams, SegmentTestRun, default_wall_tile,
                     find_owning_module, write_test_modules, build_command,
                     make_temp_modules_dir)
from knights_editor import config


# Native (platform) file dialogs are used. Note that a native dialog never
# sends the QFileDialog widget a Show event, so ModalDialogGrabGuard below
# doesn't cover it; every path that opens one must go through deferred()
# (or run at startup before any menu has been opened).
FILE_DIALOG_OPTIONS = QFileDialog.Option.ShowDirsOnly


def _has_ext(path: str, ext: str) -> bool:
    """Whether `path` has the extension `ext` (given with its dot),
    ignoring case: Knights loads INIT.LUA just as happily as init.lua on
    a case-insensitive filesystem, so the editor must too."""
    return os.path.splitext(path)[1].lower() == ext


def _is_lua(path: str) -> bool:
    return _has_ext(path, '.lua')


def release_stale_mouse_grab():
    """Drop any X pointer grab this process is still holding.

    On X11, Qt has intermittently been seen to leave an XInput2 pointer
    grab active after a menu closes (the keyboard grab is released, the
    pointer one is not). If a modal dialog then opens, every mouse click
    on the whole display is swallowed until the process exits. Qt's own
    bookkeeping does not know about the stale grab, so QWidget.releaseMouse
    alone is not enough; QWindow.setMouseGrabEnabled(False) goes down to
    the platform plugin, which on xcb issues XIUngrabDevice and thereby
    releases this client's active pointer grab whichever window took it.

    Does nothing while a popup (menu, combo box list) is open, since the
    popup legitimately owns the grab at that point.
    """
    app = QApplication.instance()
    if app is None or app.activePopupWidget() is not None:
        return
    grabber = QWidget.mouseGrabber()
    if grabber is not None:
        grabber.releaseMouse()
    # (QWindow.handle() isn't exposed to Python; a top-level widget that
    # has been created always has a platform window behind it.)
    for widget in QApplication.topLevelWidgets():
        if widget.testAttribute(Qt.WidgetAttribute.WA_WState_Created):
            win = widget.windowHandle()
            if win is not None:
                win.setMouseGrabEnabled(False)


class ModalDialogGrabGuard(QObject):
    """Application-wide event filter that releases any stale pointer grab
    just before a modal dialog is shown (see release_stale_mouse_grab).

    Install with ``app.installEventFilter(ModalDialogGrabGuard(app))``.
    Covers dialogs opened from anywhere (editors, context menus, the
    first-run setup), not just the deferred menu actions.
    """

    def eventFilter(self, obj, event):
        if (event.type() == QEvent.Type.Show
                and isinstance(obj, QDialog) and obj.isModal()):
            release_stale_mouse_grab()
        return False


def deferred(slot):
    """Wrap a slot so it runs on the next event-loop iteration.

    Menu actions are triggered from inside QMenu's mouse-release handler,
    while Qt's popup/grab bookkeeping is still mid-flight. Handlers that
    open a modal dialog are deferred so that handling fully unwinds before
    the dialog's nested event loop runs, and any pointer grab the menu
    left behind is explicitly released first.
    """
    def run(*_args):
        def go():
            release_stale_mouse_grab()
            slot()
        QTimer.singleShot(0, go)
    return run


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Knights Mod Editor")
        self.tile_table: dict[int, TileEntry] = {}
        self.gfx_cache: GraphicsCache | None = None
        self.mod_dir: str | None = None
        self.mod_loader: ModLoader | None = None
        # True when a .lua file has been saved since the last (re)load,
        # meaning the loaded tile tables may be stale.
        self._lua_changed_since_reload = False
        # Segment files created by File > New that no kts.LoadSegments
        # call references yet. Their tile table is a guess
        # (ModLoader.suggest_tile_table) registered with the loader by
        # hand, which has to be redone after every reload since each
        # reload builds a fresh loader.
        self._unreferenced_segment_files: set[str] = set()
        # Test Segment runs of the game still in progress (kept alive
        # here until each one's process exits).
        self._segment_tests: list[SegmentTestRun] = []
        # Guards against nested external-change checks (the check's own
        # dialogs shuffle window activation about).
        self._checking_external_changes = False

        self._setup_ui()
        self._setup_menus()

        # Files open in tabs may be edited by other programs while this
        # one is in the background; look for that whenever the user comes
        # back to it.
        QApplication.instance().applicationStateChanged.connect(
            self._on_application_state_changed)

    def _setup_ui(self):
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left: sidebar with a file tree and a Lua state browser
        self.file_tree = QTreeView()
        self.file_tree.setHeaderHidden(True)
        self.file_tree.doubleClicked.connect(self._on_file_double_clicked)
        self.file_tree.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.file_tree.customContextMenuRequested.connect(
            self._on_tree_context_menu)

        self.lua_state = LuaStateWidget()
        self.lua_state.open_location.connect(self._goto_location)
        self.lua_state.open_segment.connect(self._open_segment_in_editor)

        self.sidebar = QTabWidget()
        self.sidebar.setDocumentMode(True)
        self.sidebar.setTabPosition(QTabWidget.TabPosition.South)
        self.sidebar.addTab(self.file_tree, "Files")
        self.sidebar.addTab(self.lua_state, "Lua")
        self.sidebar.setMinimumWidth(160)
        splitter.addWidget(self.sidebar)

        # Center: tab widget for editors
        self.tab_widget = QTabWidget()
        self.tab_widget.setTabsClosable(True)
        self.tab_widget.tabCloseRequested.connect(self._close_tab)
        self.tab_widget.currentChanged.connect(self._on_tab_changed)
        splitter.addWidget(self.tab_widget)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        # The sidebar takes 1/5 of the window width (see resizeEvent)
        # until the user drags the splitter, after which their choice
        # sticks.
        self.splitter = splitter
        self._splitter_user_adjusted = False
        splitter.splitterMoved.connect(self._on_splitter_moved)
        self.setCentralWidget(splitter)
        self.resize(1100, 750)

        # Bottom: Find in Files panel (hidden until used)
        self.find_panel = FindInFilesPanel(self._editor_text_for_search)
        self.find_panel.open_location.connect(self._goto_match)
        self.find_panel.close_requested.connect(self._close_find_in_files)
        self.find_dock = QDockWidget("Find in Files", self)
        self.find_dock.setObjectName("find_in_files_dock")
        self.find_dock.setWidget(self.find_panel)
        self.find_dock.setAllowedAreas(Qt.DockWidgetArea.BottomDockWidgetArea)
        self.find_dock.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetClosable)
        # No title bar: the panel has its own close button, and the
        # panel/editor divider is still draggable to resize it.
        self.find_dock.setTitleBarWidget(QWidget(self.find_dock))
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea,
                           self.find_dock)
        self.find_dock.hide()

        # Permanent line/column indicator on the right of the status bar
        # (only shown while a text/Lua editor tab is active).
        self.cursor_pos_label = QLabel("")
        self.statusBar().addPermanentWidget(self.cursor_pos_label)
        # The error message currently shown (see _show_status_error), or
        # None. QMainWindow clears the status bar's message whenever a
        # status-tip event arrives (hovering over menu items sends an
        # empty one), which would leave the red bar empty; it's restored
        # from here.
        self._status_error: str | None = None
        self.statusBar().messageChanged.connect(self._on_status_message_changed)

    def _on_splitter_moved(self, pos: int, index: int):
        self._splitter_user_adjusted = True

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not self._splitter_user_adjusted:
            total = self.splitter.width()
            sidebar = total // 5
            self.splitter.setSizes([sidebar, total - sidebar])

    def _setup_menus(self):
        menu_bar = self.menuBar()

        # File menu
        file_menu = menu_bar.addMenu("&File")

        # Opening
        open_mod_action = QAction("Open &Mod...", self)
        open_mod_action.setShortcut(QKeySequence("Ctrl+Shift+O"))
        open_mod_action.triggered.connect(deferred(self._open_mod))
        file_menu.addAction(open_mod_action)

        open_action = QAction("Open &File...", self)
        open_action.setShortcut(QKeySequence.StandardKey.Open)
        open_action.triggered.connect(deferred(self._open_any_file))
        file_menu.addAction(open_action)

        # Rebuilt from the saved lists each time it is about to be shown.
        self.recent_menu = file_menu.addMenu("Open &Recent")
        self.recent_menu.aboutToShow.connect(self._populate_recent_menu)

        file_menu.addSeparator()

        # Saving
        save_action = QAction("&Save", self)
        save_action.setShortcut(QKeySequence.StandardKey.Save)
        save_action.triggered.connect(deferred(self._save_current))
        file_menu.addAction(save_action)

        save_all_action = QAction("Save &All", self)
        save_all_action.setShortcut(QKeySequence("Ctrl+Shift+S"))
        save_all_action.triggered.connect(deferred(self._save_all))
        file_menu.addAction(save_all_action)

        file_menu.addSeparator()

        # Discarding changes / reloading from disk
        reload_action = QAction("Re&load Mod", self)
        reload_action.setShortcut(QKeySequence("F5"))
        reload_action.triggered.connect(deferred(self._reload_mod))
        file_menu.addAction(reload_action)

        revert_action = QAction("Re&vert File", self)
        revert_action.triggered.connect(deferred(self._revert_current))
        file_menu.addAction(revert_action)

        file_menu.addSeparator()

        # Configuration
        load_order_action = QAction("Module Load &Order...", self)
        load_order_action.triggered.connect(deferred(self._module_load_order))
        file_menu.addAction(load_order_action)

        settings_action = QAction("Se&ttings...", self)
        settings_action.triggered.connect(deferred(self._edit_settings))
        file_menu.addAction(settings_action)

        file_menu.addSeparator()

        quit_action = QAction("&Quit", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        # Edit menu
        edit_menu = menu_bar.addMenu("&Edit")

        undo_action = QAction("&Undo", self)
        undo_action.setShortcut(QKeySequence.StandardKey.Undo)
        undo_action.triggered.connect(self._undo)
        edit_menu.addAction(undo_action)

        redo_action = QAction("&Redo", self)
        redo_action.setShortcut(QKeySequence.StandardKey.Redo)
        redo_action.triggered.connect(self._redo)
        edit_menu.addAction(redo_action)

        edit_menu.addSeparator()

        # Text editors handle these keys themselves when focused (they
        # override the shortcuts); the actions reach them via the menu,
        # and reach segment editors' map selections either way.
        # (Mnemonics chosen to avoid the search actions' T and P.)
        cut_action = QAction("&Cut", self)
        cut_action.setShortcut(QKeySequence.StandardKey.Cut)
        cut_action.triggered.connect(self._cut)
        edit_menu.addAction(cut_action)

        copy_action = QAction("C&opy", self)
        copy_action.setShortcut(QKeySequence.StandardKey.Copy)
        copy_action.triggered.connect(self._copy)
        edit_menu.addAction(copy_action)

        paste_action = QAction("Pa&ste", self)
        paste_action.setShortcut(QKeySequence.StandardKey.Paste)
        paste_action.triggered.connect(self._paste)
        edit_menu.addAction(paste_action)

        edit_menu.addSeparator()

        find_action = QAction("&Find...", self)
        find_action.setShortcut(QKeySequence.StandardKey.Find)
        find_action.triggered.connect(self._find)
        edit_menu.addAction(find_action)

        find_next_action = QAction("Find &Next", self)
        # Explicit F3 rather than StandardKey.FindNext: on some platforms
        # the standard key also maps to Ctrl+G, clashing with Go to Line.
        find_next_action.setShortcut(QKeySequence("F3"))
        find_next_action.triggered.connect(self._find_next)
        edit_menu.addAction(find_next_action)

        find_prev_action = QAction("Find &Previous", self)
        find_prev_action.setShortcut(QKeySequence("Shift+F3"))
        find_prev_action.triggered.connect(self._find_previous)
        edit_menu.addAction(find_prev_action)

        goto_action = QAction("&Go to Line...", self)
        goto_action.setShortcut(QKeySequence("Ctrl+G"))
        goto_action.triggered.connect(deferred(self._goto_line))
        edit_menu.addAction(goto_action)

        edit_menu.addSeparator()

        find_files_action = QAction("Find in F&iles...", self)
        find_files_action.setShortcut(QKeySequence("Ctrl+Shift+F"))
        find_files_action.triggered.connect(self._find_in_files)
        edit_menu.addAction(find_files_action)

        next_result_action = QAction("Next Search Resul&t", self)
        next_result_action.setShortcut(QKeySequence("F4"))
        next_result_action.triggered.connect(
            lambda: self._step_search_result(1))
        edit_menu.addAction(next_result_action)

        prev_result_action = QAction("Previous Search Res&ult", self)
        prev_result_action.setShortcut(QKeySequence("Shift+F4"))
        prev_result_action.triggered.connect(
            lambda: self._step_search_result(-1))
        edit_menu.addAction(prev_result_action)

        # View menu
        view_menu = menu_bar.addMenu("&View")

        panel_action = self.find_dock.toggleViewAction()
        panel_action.setText("Find in Files &Panel")
        view_menu.addAction(panel_action)
        view_menu.addSeparator()

        zoom_in_action = QAction("Zoom &In", self)
        zoom_in_action.setShortcuts([QKeySequence.StandardKey.ZoomIn,
                                     QKeySequence("Ctrl+=")])
        zoom_in_action.triggered.connect(self._zoom_in)
        view_menu.addAction(zoom_in_action)

        zoom_out_action = QAction("Zoom &Out", self)
        zoom_out_action.setShortcut(QKeySequence.StandardKey.ZoomOut)
        zoom_out_action.triggered.connect(self._zoom_out)
        view_menu.addAction(zoom_out_action)

        zoom_reset_action = QAction("&Reset Zoom", self)
        zoom_reset_action.setShortcut(QKeySequence("Ctrl+0"))
        zoom_reset_action.triggered.connect(self._zoom_reset)
        view_menu.addAction(zoom_reset_action)

        # Segment menu
        seg_menu = menu_bar.addMenu("&Segment")

        add_action = QAction("&Add Segment", self)
        add_action.setShortcut(QKeySequence("Ctrl+I"))
        add_action.triggered.connect(deferred(self._add_segment))
        seg_menu.addAction(add_action)

        del_action = QAction("&Delete Segment", self)
        del_action.setShortcut(QKeySequence("Ctrl+D"))
        del_action.triggered.connect(deferred(self._delete_segment))
        seg_menu.addAction(del_action)

        seg_menu.addSeparator()

        test_action = QAction("&Test Segment", self)
        test_action.setShortcut(QKeySequence("Ctrl+T"))
        test_action.triggered.connect(deferred(self._test_segment))
        seg_menu.addAction(test_action)

        # Help menu
        help_menu = menu_bar.addMenu("&Help")

        lua_docs_action = QAction("Knights &Lua Documentation", self)
        lua_docs_action.triggered.connect(self._open_lua_docs)
        help_menu.addAction(lua_docs_action)

        about_action = QAction("&About", self)
        about_action.triggered.connect(deferred(self._show_about))
        help_menu.addAction(about_action)

    # --- Help ---

    def _open_lua_docs(self):
        """Help > Knights Lua Documentation: open the docs in a browser."""
        QDesktopServices.openUrl(QUrl(LUA_DOCS_URL))

    def _show_about(self):
        """Help > About."""
        AboutDialog(self).exec()

    # --- Settings ---

    def _show_settings(self, message: str | None = None) -> bool:
        """Run the Settings dialog and, if the user presses OK, store the
        new values. `message` explains why the dialog opened when it
        wasn't from the menu. Returns whether the user pressed OK; does
        not reload anything (see _edit_settings for that)."""
        dlg = SettingsDialog(self, config.get_knights_data_dir(),
                             config.get_knights_binary(),
                             config.get_extra_modules_dirs(), message)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return False
        config.set_knights_data_dir(dlg.knights_data_dir())
        config.set_knights_binary(dlg.knights_binary())
        config.set_extra_modules_dirs(dlg.extra_modules_dirs())
        return True

    def _edit_settings(self):
        """File > Settings. If the settings that affect how mods load
        (data directory, additional modules directories) changed while a
        mod is open, reload it so the editor reflects them."""
        before = (config.get_knights_data_dir(),
                  config.get_extra_modules_dirs())
        if not self._show_settings():
            return
        after = (config.get_knights_data_dir(),
                 config.get_extra_modules_dirs())
        if self.mod_dir is None or before == after:
            return
        self._update_find_roots()
        self._reload_mod(interactive=True)

    def ensure_knights_data_dir(self) -> str | None:
        """Get the configured knights_data directory, prompting if unset."""
        data_dir = config.get_knights_data_dir()
        if data_dir and os.path.isdir(data_dir):
            return data_dir
        return self.prompt_knights_data_dir()

    def prompt_knights_data_dir(self) -> str | None:
        """Ask the user to set the knights_data directory (via the
        Settings dialog). Returns the directory, or None if the user
        cancelled or set one that doesn't exist."""
        current = config.get_knights_data_dir()
        if current:
            message = (f"The Knights data directory '{current}' does not "
                       "exist. Please set the knights_data directory (the "
                       "folder that contains the 'modules' subfolder).")
        else:
            message = ("Please set your knights_data directory (the folder "
                       "that contains the 'modules' subfolder). This is "
                       "needed so the editor can load mods and their "
                       "dependencies. You can change it later via "
                       "File > Settings.")
        if not self._show_settings(message):
            return None
        data_dir = config.get_knights_data_dir()
        return data_dir if data_dir and os.path.isdir(data_dir) else None

    def open_mod(self, mod_dir: str):
        """Open a mod directory."""
        # Normalise so a relative path from the command line doesn't end
        # up in the recent-mods list or handed to the game as-is.
        mod_dir = os.path.abspath(mod_dir)
        if not os.path.isfile(os.path.join(mod_dir, "init.lua")):
            QMessageBox.warning(self, "Not a mod",
                                f"No init.lua found in {mod_dir}")
            return

        knights_data = self.ensure_knights_data_dir()
        if knights_data is None:
            QMessageBox.warning(
                self, "No Knights Data Directory",
                "Cannot open the mod: no knights_data directory is set.")
            return

        self.mod_dir = mod_dir
        mod_name = os.path.basename(os.path.normpath(mod_dir))
        config.add_recent_mod(mod_dir)

        # Load the mod's Lua to get tile definitions (along with the
        # other mods in its load order, see _load_mod_lua).
        loader, load_error = self._load_mod_lua(knights_data, mod_dir)
        if load_error is not None:
            QMessageBox.warning(
                self, "Error loading mod",
                f"An error occurred while loading the mod's Lua:\n\n"
                f"{load_error}\n\nTile graphics may be missing or incomplete.")
        self.mod_loader = loader
        self.tile_table = loader.get_tile_table()
        self.gfx_cache = GraphicsCache(mod_dir)
        self._lua_changed_since_reload = False
        self._unreferenced_segment_files.clear()
        self.lua_state.set_state(loader, mod_dir, self.gfx_cache)

        # Set up file tree
        fs_model = QFileSystemModel()
        fs_model.setRootPath(mod_dir)
        self.file_tree.setModel(fs_model)
        self.file_tree.setRootIndex(fs_model.index(mod_dir))
        # Hide all columns except name
        for col in range(1, fs_model.columnCount()):
            self.file_tree.hideColumn(col)

        # Close all open tabs
        while self.tab_widget.count() > 0:
            self._discard_tab(0)

        self.setWindowTitle(f"Knights Mod Editor - {mod_name}")
        self._update_find_roots()

        if load_error is not None:
            self._show_lua_error_location(load_error, loader.loaded_files)
        else:
            self._show_status(self._describe_load_order(loader))

    def _confirm_open_mod(self) -> bool:
        """Opening a mod closes every tab, so give the user a chance to
        save (or back out) rather than silently discarding edits.
        Returns False if the user cancelled."""
        return not (self._any_unsaved() and self._prompt_save_all(
            "Save changes before opening another mod?",
            "Opening a mod closes all open files, so unsaved changes "
            "would be lost.",
            "Discard Changes",
            "The mod was not opened.") == 'cancel')

    def _open_mod(self):
        """Open mod directory dialog."""
        if not self._confirm_open_mod():
            return
        data_dir = self.ensure_knights_data_dir()
        if data_dir is None:
            return
        # Start in the folder containing the most recently opened mod, so
        # that mod and its siblings are one double-click away; fall back to
        # knights_data/modules for a first-time user.
        start_dir = None
        for recent in config.get_recent_mods():
            parent = os.path.dirname(os.path.normpath(recent))
            if os.path.isdir(parent):
                start_dir = parent
                break
        if start_dir is None:
            start_dir = os.path.join(data_dir, "modules")
            if not os.path.isdir(start_dir):
                start_dir = data_dir
        dir_path = QFileDialog.getExistingDirectory(
            self, "Open Mod Directory", start_dir, FILE_DIALOG_OPTIONS)
        if dir_path:
            self.open_mod(dir_path)

    def _open_any_file(self):
        """File > Open: open any file from anywhere in an editor tab (a
        mod's readme, for instance), without changing the open mod."""
        start_dir = None
        for recent in config.get_recent_files():
            parent = os.path.dirname(recent)
            if os.path.isdir(parent):
                start_dir = parent
                break
        if start_dir is None:
            start_dir = self.mod_dir if self.mod_dir else os.path.expanduser("~")
        path, _ = QFileDialog.getOpenFileName(
            self, "Open File", start_dir,
            "All Files (*);;Lua Files (*.lua);;Text Files (*.txt)")
        if path:
            self._open_recent_file(path)

    def _populate_recent_menu(self):
        """Fill the Open Recent submenu: mods first, then files, with a
        separator between the two groups (as in VS Code)."""
        menu = self.recent_menu
        menu.clear()
        mods = config.get_recent_mods()
        files = config.get_recent_files()
        for mod_dir in mods:
            action = menu.addAction(self._recent_label(mod_dir))
            action.setToolTip(mod_dir)
            action.triggered.connect(
                deferred(lambda p=mod_dir: self._open_recent_mod(p)))
        if mods and files:
            menu.addSeparator()
        for path in files:
            action = menu.addAction(self._recent_label(path))
            action.setToolTip(path)
            action.triggered.connect(
                deferred(lambda p=path: self._open_recent_file(p)))
        if mods or files:
            menu.addSeparator()
            clear = menu.addAction("&Clear Recently Opened")
            clear.triggered.connect(lambda *_: config.clear_recent())
        else:
            empty = menu.addAction("(none)")
            empty.setEnabled(False)

    @staticmethod
    def _recent_label(path: str) -> str:
        """Menu text for a recent entry: the name, followed by where it
        lives (with the home directory abbreviated to ~). Ampersands are
        doubled so they aren't taken as accelerators."""
        path = os.path.normpath(path)
        name = os.path.basename(path)
        parent = os.path.dirname(path)
        home = os.path.expanduser("~")
        if parent == home or parent.startswith(home + os.sep):
            parent = "~" + parent[len(home):]
        return f"{name}  ({parent})".replace("&", "&&")

    def _open_recent_mod(self, mod_dir: str):
        if not os.path.isdir(mod_dir):
            config.remove_recent_mod(mod_dir)
            QMessageBox.warning(
                self, "Mod Not Found",
                f"{mod_dir} no longer exists.\n\n"
                "It has been removed from the Open Recent list.")
            return
        if not self._confirm_open_mod():
            return
        self.open_mod(mod_dir)

    def _open_recent_file(self, path: str):
        if not os.path.isfile(path):
            config.remove_recent_file(path)
            QMessageBox.warning(
                self, "File Not Found",
                f"{path} no longer exists.\n\n"
                "It has been removed from the Open Recent list.")
            return
        editor = self._open_or_focus_file(path)
        if editor is None:
            QMessageBox.warning(
                self, "Cannot Open File",
                f"{path} could not be opened as text.")
            return
        config.add_recent_file(path)

    def _update_find_roots(self):
        """Tell the Find in Files panel where its scopes search: the open
        mod; the mods in the current load order (from the last load, so
        after a failed load only those that got loaded, plus this mod);
        and every mod installed in any modules directory. The edited mod
        always searches from its own directory, even if a mod of the
        same name is installed."""
        if self.mod_dir is None:
            self.find_panel.set_roots(None, [], [])
            return
        mod_name = os.path.basename(os.path.normpath(self.mod_dir))
        loaded = (dict(self.mod_loader.get_mod_dirs())
                  if self.mod_loader is not None else {})
        loaded[mod_name] = self.mod_dir
        installed = {}
        knights_data = config.get_knights_data_dir()
        if knights_data:
            extra_dirs = config.get_extra_modules_dirs()
            path = modules_search_path(knights_data, extra_dirs, self.mod_dir)
            for name in list_installed_modules(knights_data, extra_dirs,
                                               self.mod_dir):
                installed[name] = find_module_dir(name, path)
        installed[mod_name] = self.mod_dir
        self.find_panel.set_roots(self.mod_dir, list(loaded.items()),
                                  sorted(installed.items()))

    def _show_status(self, message: str, timeout_ms: int = 5000):
        """Show a normal status bar message, clearing any error highlight."""
        self._status_error = None
        bar = self.statusBar()
        bar.setStyleSheet("")
        bar.showMessage(message, timeout_ms)

    def _show_status_error(self, message: str):
        """Show a highlighted error message in the status bar.

        Stays until the next _show_status call (e.g. a successful reload)
        replaces it.
        """
        self._status_error = message
        bar = self.statusBar()
        bar.setStyleSheet(
            "QStatusBar { background-color: #aa0000; color: white; "
            "font-weight: bold; }")
        bar.showMessage(message)

    def _on_status_message_changed(self, text: str):
        # A menu status tip cleared the message; put the error back so
        # the red bar is never left blank.
        if not text and self._status_error is not None:
            self.statusBar().showMessage(self._status_error)

    def _reload_mod(self, checked=False, interactive: bool = True):
        """Re-execute the mod's Lua and push the new tile tables into all
        open segment editors.

        Segment data, selection and undo history in open editors are
        preserved; only the tile tables (and graphics) are refreshed. If
        the Lua fails to load, the previous state is kept so a
        half-finished edit doesn't blank out open maps.
        """
        if self.mod_dir is None:
            return
        # The reload reads from disk, so unsaved edits can't be included.
        # Offer to save everything first; the alternative is to discard
        # the edits (reverting the editors to disk), so the editors never
        # end up out of step with the reloaded state.
        if interactive and self._any_unsaved():
            choice = self._prompt_save_all(
                "Save changes before reloading the mod?",
                "The reload reads files from disk. Discarding reverts "
                "all open files to their last saved state.",
                "Discard Changes",
                "The mod was not reloaded.")
            if choice == 'cancel':
                return
            if choice == 'proceed':
                for editor in self._unsaved_editors():
                    try:
                        editor.revert()
                    except Exception as e:
                        QMessageBox.warning(
                            self, "Error reverting file",
                            f"Could not re-read "
                            f"{os.path.basename(editor.filepath)}:\n\n{e}\n\n"
                            "The mod was not reloaded.")
                        return
        knights_data = self.ensure_knights_data_dir()
        if knights_data is None:
            return

        loader, e = self._load_mod_lua(knights_data, self.mod_dir)
        if e is not None:
            _, _, err = parse_lua_error(e, loader.loaded_files)
            if len(err) > 120:
                err = err[:120] + "..."
            # (The Lua state panel keeps showing the last successful load.)
            if interactive:
                self._show_status_error(f"Reload failed: {err}")
                # Jump to the offending line (opening the file if needed)
                # so the user can fix it straight away.
                self._show_lua_error_location(e, loader.loaded_files)
                QMessageBox.warning(
                    self, "Error reloading mod",
                    f"An error occurred while reloading the mod's Lua:\n\n{e}")
            else:
                # An auto-reload after a save. The user may well be partway
                # through a larger edit and have saved in a non-working
                # state, so don't yank the cursor away or pop up a dialog;
                # just flag it and let them ask for details when ready.
                self._show_status_error(
                    "Auto-reload failed (press F5 for error details)")
            return

        self.mod_loader = loader
        self.tile_table = loader.get_tile_table()
        # Fresh cache: graphics files or color keys may have changed, and
        # tile pixmaps are keyed by (old) tile table identity anyway.
        self.gfx_cache = GraphicsCache(self.mod_dir)
        self._lua_changed_since_reload = False
        # The fresh loader only knows files that Lua code actually
        # referenced, so any "new" file it knows about is new no longer.
        self._unreferenced_segment_files = {
            p for p in self._unreferenced_segment_files
            if loader.get_file_tile_table(p) is None}
        self.lua_state.set_state(loader, self.mod_dir, self.gfx_cache)
        self._update_find_roots()

        for i in range(self.tab_widget.count()):
            widget = self.tab_widget.widget(i)
            if isinstance(widget, SegmentEditor):
                tile_table = self._file_tile_table(widget.filepath)
                if tile_table is None:
                    tile_table = self.tile_table
                widget.set_tile_table(tile_table, self.gfx_cache)

        self._show_status(
            "Mod reloaded. " + self._describe_load_order(loader))

    def _load_mod_lua(self, knights_data: str,
                      mod_dir: str) -> tuple[ModLoader, Exception | None]:
        """Load the mod in `mod_dir` together with the other mods in its
        module load order, one after another as the game does.

        The order is the user's explicit choice for this mod (File >
        Module Load Order), or, by default, inferred by a preliminary
        on-demand load (mod_loader.discover_load_order) whose Lua state
        is then discarded.

        Returns (loader, error). On error the loader holds whatever was
        loaded before the failure; loader.load_order_auto records which
        kind of order was used.
        """
        extra_dirs = config.get_extra_modules_dirs()
        order = config.get_mod_load_order(mod_dir)
        auto = order is None
        if auto:
            order = discover_load_order(knights_data, mod_dir, extra_dirs).order
        else:
            mod_name = os.path.basename(os.path.normpath(mod_dir))
            if mod_name not in order:
                order = list(order) + [mod_name]
        loader = ModLoader(knights_data, extra_modules_dirs=extra_dirs,
                           mod_dir=mod_dir)
        loader.load_order_auto = auto
        try:
            loader.load_sequence(order, mod_dir)
        except Exception as e:
            return loader, e
        return loader, None

    @staticmethod
    def _describe_load_order(loader: ModLoader) -> str:
        kind = "inferred" if loader.load_order_auto else "explicit"
        return f"Load order ({kind}): {', '.join(loader.load_order)}"

    def _module_load_order(self):
        """File > Module Load Order: let the user pick which installed
        mods load with this one, and in what order, then reload."""
        if self.mod_dir is None:
            QMessageBox.information(
                self, "Module Load Order",
                "Open a mod first (File > Open Mod).")
            return
        knights_data = self.ensure_knights_data_dir()
        if knights_data is None:
            return
        mod_name = os.path.basename(os.path.normpath(self.mod_dir))
        # Infer afresh (rather than reuse the last load's result) so the
        # dialog reflects the Lua as it is on disk right now.
        extra_dirs = config.get_extra_modules_dirs()
        discovery = discover_load_order(knights_data, self.mod_dir, extra_dirs)
        installed = list_installed_modules(knights_data, extra_dirs, self.mod_dir)
        saved = config.get_mod_load_order(self.mod_dir)
        dlg = LoadOrderDialog(self, mod_name, installed, discovery, saved)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        chosen = dlg.result_order()
        if chosen == saved:
            return
        config.set_mod_load_order(self.mod_dir, chosen)
        self._reload_mod(interactive=True)

    def _show_lua_error_location(self, exc: BaseException, known_files=()):
        """If a Lua load error names a file and line, open (or focus) that
        file and move the cursor to the line. Does nothing for errors
        without a usable location. `known_files` are the paths the
        failed loader read, needed to resolve paths Lua abbreviated."""
        filepath, line, _ = parse_lua_error(exc, known_files)
        if filepath is None:
            return
        editor = self._open_or_focus_file(filepath)
        if isinstance(editor, TextEditor):
            editor.goto_line_number(line)

    def _goto_location(self, filepath: str, line: int):
        """Open (or focus) a source file and move to `line` (1-based;
        0 or less leaves the cursor alone). Used by the Lua state
        browser's go-to-definition links."""
        editor = self._open_or_focus_file(filepath)
        if isinstance(editor, TextEditor) and line > 0:
            editor.goto_line_number(line)
            editor.setFocus()

    def _open_segment_in_editor(self, segment):
        """Open (or focus) the segments file a Segment was loaded from
        and select that segment. Used by the Lua state browser's
        segment links."""
        path = getattr(segment, 'source_file', None)
        if not path or not os.path.isfile(path):
            self._show_status_error(
                f"No source file known for segment '{segment.name}'")
            return
        editor = self._open_or_focus_file(path)
        if not isinstance(editor, SegmentEditor):
            self._show_status_error(
                f"Could not open {os.path.basename(path)} as a "
                "segments file")
            return
        if not editor.select_segment(segment.name, segment.source_index):
            self._show_status_error(
                f"Segment '{segment.name}' not found in "
                f"{os.path.basename(path)} (file changed since load?)")

    def _any_unsaved(self) -> bool:
        return any(getattr(self.tab_widget.widget(i), 'unsaved', False)
                   for i in range(self.tab_widget.count()))

    def _unsaved_editors(self) -> list:
        return [self.tab_widget.widget(i)
                for i in range(self.tab_widget.count())
                if getattr(self.tab_widget.widget(i), 'unsaved', False)]

    def _prompt_save_all(self, question: str, explanation: str,
                         proceed_label: str, not_done_text: str) -> str:
        """Ask whether to save all unsaved files before some action that
        would otherwise lose or ignore them (reloading, opening another
        mod).

        `question` is the headline, `explanation` says why it matters,
        `proceed_label` is the destructive "don't save" button text and
        `not_done_text` is appended to the error shown if a save fails.

        Returns 'save' if all files were saved, 'proceed' if the user
        chose the destructive option (nothing saved; the caller decides
        what that means), or 'cancel' if the user cancelled or a save
        failed.
        """
        editors = self._unsaved_editors()
        names = "\n".join(
            "    " + os.path.basename(e.filepath) for e in editors)
        msg = QMessageBox(self)
        msg.setWindowTitle("Unsaved Changes")
        msg.setText(question)
        msg.setInformativeText(
            explanation + "\n\nUnsaved files:\n" + names)
        msg.setIcon(QMessageBox.Icon.Warning)
        save_btn = msg.addButton("Save All", QMessageBox.ButtonRole.AcceptRole)
        msg.addButton(proceed_label, QMessageBox.ButtonRole.DestructiveRole)
        cancel_btn = msg.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        msg.setDefaultButton(save_btn)
        msg.exec()
        if msg.clickedButton() == cancel_btn:
            return 'cancel'
        if msg.clickedButton() == save_btn:
            for editor in editors:
                try:
                    editor.save()
                except Exception as e:
                    QMessageBox.warning(
                        self, "Error saving file",
                        f"Could not save "
                        f"{os.path.basename(editor.filepath)}:\n\n{e}\n\n"
                        + not_done_text)
                    return 'cancel'
                self._update_tab_title(editor)
                self._note_saved(editor)
            return 'save'
        return 'proceed'

    def _note_saved(self, editor):
        """Record that an editor was saved, for auto-reload tracking."""
        if _is_lua(editor.filepath):
            self._lua_changed_since_reload = True

    def _maybe_auto_reload(self):
        """Auto-reload once no unsaved files remain (so the reload sees
        what's on screen, not stale on-disk copies of unsaved files)."""
        if self._lua_changed_since_reload and not self._any_unsaved():
            self._reload_mod(interactive=False)

    def _on_tree_context_menu(self, pos):
        model = self.file_tree.model()
        if self.mod_dir is None or model is None:
            return
        # Right-clicking a file targets its containing directory;
        # right-clicking blank space targets the mod root.
        index = self.file_tree.indexAt(pos)
        if index.isValid():
            path = model.filePath(index)
            target_dir = path if os.path.isdir(path) else os.path.dirname(path)
        else:
            target_dir = self.mod_dir

        menu = QMenu(self)
        new_lua = menu.addAction("New Lua File...")
        new_seg = menu.addAction("New Segments File...")
        chosen = menu.exec(self.file_tree.viewport().mapToGlobal(pos))
        if chosen == new_lua:
            self._new_file(target_dir, ".lua")
        elif chosen == new_seg:
            self._new_file(target_dir, ".txt")

    def _new_file(self, target_dir: str, ext: str):
        kind = "Lua" if ext == ".lua" else "Segments"
        rel = os.path.relpath(target_dir, self.mod_dir)
        where = os.path.basename(self.mod_dir) if rel == "." else rel
        name, ok = QInputDialog.getText(
            self, f"New {kind} File", f"File name (in {where}/):")
        if not ok or not name.strip():
            return
        name = name.strip()
        if not os.path.splitext(name)[1]:
            name += ext
        filepath = os.path.join(target_dir, name)
        if os.path.exists(filepath):
            QMessageBox.warning(self, "File exists",
                                f"{name} already exists.")
            return

        snippet_info = None
        try:
            if ext == ".lua":
                with open(filepath, "w", encoding="utf-8"):
                    pass
            else:
                snippet_info = self._create_segments_file(filepath)
                if snippet_info is None:
                    return
        except OSError as e:
            QMessageBox.warning(self, "Error creating file", str(e))
            return

        self._open_file(filepath)
        if snippet_info is not None:
            self._show_snippet_dialog(*snippet_info)

    def _file_tile_table(self, filepath: str) -> dict[int, TileEntry] | None:
        """The tile table for the segment file `filepath`, or None if
        the global fallback (self.tile_table) should be used.

        Files referenced by a kts.LoadSegments call get that call's
        table. Files created by File > New that nothing references yet
        (see _create_segments_file) get the loader's suggested table,
        registered with the current loader on demand so the answer
        survives reloads (_reload_mod stops treating such a file as new
        once a reload finds a real LoadSegments call for it).
        """
        loader = self.mod_loader
        if loader is None:
            return None
        tile_table = loader.get_file_tile_table(filepath)
        if tile_table is not None:
            return tile_table
        if os.path.realpath(filepath) not in self._unreferenced_segment_files:
            return None
        _, lua_table = loader.suggest_tile_table(self.mod_dir)
        if lua_table is None:
            return None
        loader.register_segment_file(filepath, lua_table)
        return loader.get_file_tile_table(filepath)

    def _create_segments_file(self, filepath: str) -> tuple[str, str] | None:
        """Write a new segments file containing one starter segment.

        The tile table is taken from the mod's last existing
        kts.LoadSegments call if there is one (the file is noted as
        unreferenced so that _file_tile_table registers that table with
        the loader, now and again after each reload, until some Lua
        code references the file); otherwise the global fallback table
        applies. Returns
        (snippet, note) for the snippet dialog, or None on failure
        (a message box has been shown).
        """
        expr, calls = "tile_table", []
        if self.mod_loader is not None:
            expr, _ = self.mod_loader.suggest_tile_table(self.mod_dir)
            calls = self.mod_loader.get_mod_load_segments_calls(self.mod_dir)

        self._unreferenced_segment_files.add(os.path.realpath(filepath))
        tile_table = self._file_tile_table(filepath)
        if tile_table is None:
            tile_table = self.tile_table

        fill = default_fill_tile(tile_table)
        if fill is None:
            QMessageBox.warning(
                self, "No tile table",
                "Cannot create a segments file: the mod's tile table is "
                "empty (or the mod defines no tile table at all).")
            return None

        seg = Segment(name="new_1", width=12, height=12,
                      data=[[fill] * 12 for _ in range(12)])
        save_segments(filepath, [seg])

        relname = os.path.relpath(filepath, self.mod_dir).replace(os.sep, "/")
        snippet = f'kts.LoadSegments({expr}, "{relname}")'
        note = ("After pasting, save the Lua file and the editor will "
                "reload the mod automatically.")
        if calls:
            c = calls[-1]
            src = os.path.relpath(c.source, self.mod_dir)
            note += (f"\n\nFor reference, the mod's existing "
                     f"kts.LoadSegments calls end at {src}, line {c.line}.")
        return snippet, note

    def _show_snippet_dialog(self, snippet: str, note: str):
        dlg = QDialog(self)
        dlg.setWindowTitle("Lua Code Snippet")
        layout = QVBoxLayout(dlg)

        intro = QLabel(
            "To have the game load the new file, paste this line into "
            "the mod's Lua code, at a point where the tile table has "
            "been defined:")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        text = QPlainTextEdit(snippet)
        text.setReadOnly(True)
        font = QFont("Monospace")
        font.setStyleHint(QFont.StyleHint.TypeWriter)
        text.setFont(font)
        text.setFixedHeight(3 * text.fontMetrics().height())
        layout.addWidget(text)

        note_label = QLabel(note)
        note_label.setWordWrap(True)
        layout.addWidget(note_label)

        btns = QHBoxLayout()
        copy_btn = QPushButton("Copy to Clipboard")

        def do_copy():
            QGuiApplication.clipboard().setText(snippet)
            copy_btn.setText("Copied")

        copy_btn.clicked.connect(do_copy)
        close_btn = QPushButton("Close")
        close_btn.setDefault(True)
        close_btn.clicked.connect(dlg.accept)
        btns.addWidget(copy_btn)
        btns.addStretch()
        btns.addWidget(close_btn)
        layout.addLayout(btns)

        dlg.setMinimumWidth(480)
        dlg.exec()

    def _on_file_double_clicked(self, index: QModelIndex):
        """Handle double-click on a file in the tree."""
        model = self.file_tree.model()
        if not model:
            return
        filepath = model.filePath(index)
        if os.path.isdir(filepath):
            return
        self._open_or_focus_file(filepath)

    def _find_open_tab(self, filepath: str):
        """Return the editor widget for `filepath` if it's open in a tab."""
        target = os.path.normcase(os.path.realpath(filepath))
        for i in range(self.tab_widget.count()):
            widget = self.tab_widget.widget(i)
            path = getattr(widget, 'filepath', None)
            if path is not None and \
                    os.path.normcase(os.path.realpath(path)) == target:
                return widget
        return None

    def _open_or_focus_file(self, filepath: str):
        """Switch to the tab for `filepath`, opening it if necessary.
        Returns the editor widget (None if the file couldn't be opened)."""
        editor = self._find_open_tab(filepath)
        if editor is not None:
            self.tab_widget.setCurrentWidget(editor)
            return editor
        return self._open_file(filepath)

    def _open_file(self, filepath: str):
        """Open a file in a new tab. Returns the editor widget, or None
        if the file couldn't be opened, in which case the user has been
        told why: a dialog for the file types the editor is for (.lua
        and .txt), just a status bar message for anything else, where
        the likely cause is double-clicking a binary file."""
        basename = os.path.basename(filepath)
        is_txt = _has_ext(filepath, '.txt')
        is_text_type = _is_lua(filepath) or is_txt

        try:
            editor = None
            if _is_lua(filepath):
                editor = LuaEditor(filepath)
            elif is_txt:
                # Try to parse as segment file
                editor = self._try_open_as_segments(filepath)
            if editor is None:
                # Plain text for anything else
                editor = TextEditor(filepath)
        except NotUtf8Error as e:
            self._report_open_failure(filepath, e.reason, is_text_type)
            return None
        except OSError as e:
            self._report_open_failure(filepath, e.strerror or str(e), is_text_type)
            return None

        if isinstance(editor, TextEditor):
            self._connect_text_editor(editor)
        self.tab_widget.addTab(editor, basename)
        self.tab_widget.setCurrentWidget(editor)
        return editor

    def _report_open_failure(self, filepath: str, reason: str, dialog: bool):
        message = f"Could not open {os.path.basename(filepath)}: {reason}"
        if dialog:
            QMessageBox.warning(self, "Cannot Open File", message)
        else:
            self._show_status(message)

    def _connect_text_editor(self, editor: TextEditor):
        editor.modified_changed.connect(lambda _: self._update_tab_title(editor))
        editor.cursorPositionChanged.connect(self._update_cursor_pos_label)

    def _try_open_as_segments(self, filepath: str):
        """Try to open a .txt file as a segment editor. Returns None if
        the file isn't a segment file (or can't be opened as one, in
        which case the user is told why)."""
        try:
            segments = load_segments(filepath)
            if not segments:
                return None
        except NotSegmentFileError:
            return None
        except SegmentFormatError as e:
            QMessageBox.warning(
                self, "Cannot open as segments",
                f"{os.path.basename(filepath)} looks like a segment file "
                f"but is not laid out the way the editor expects, so it "
                f"will be opened as plain text instead:\n\n{e}")
            return None
        except Exception:
            return None
        try:
            # Prefer the tile table that was passed to kts.LoadSegments for
            # this particular file; fall back to the global tile_table for
            # files not referenced by any loaded mod.
            tile_table = self._file_tile_table(filepath)
            if tile_table is None:
                tile_table = self.tile_table
            editor = SegmentEditor(filepath, tile_table, self.gfx_cache)
            editor.modified_changed.connect(lambda _: self._update_tab_title(editor))
            return editor
        except Exception:
            return None

    def _update_tab_title(self, editor):
        """Update tab title to show unsaved indicator."""
        idx = self.tab_widget.indexOf(editor)
        if idx < 0:
            return
        basename = os.path.basename(editor.filepath)
        if editor.unsaved:
            self.tab_widget.setTabText(idx, basename + " *")
        else:
            self.tab_widget.setTabText(idx, basename)

    def _close_tab(self, index: int):
        """Close a tab, prompting to save if unsaved."""
        widget = self.tab_widget.widget(index)
        if hasattr(widget, 'unsaved') and widget.unsaved:
            msg = QMessageBox(self)
            msg.setWindowTitle("Unsaved Changes")
            msg.setText(f"Save changes to {os.path.basename(widget.filepath)}?")
            msg.setIcon(QMessageBox.Icon.Warning)
            save_btn = msg.addButton("Save", QMessageBox.ButtonRole.AcceptRole)
            msg.addButton("Discard", QMessageBox.ButtonRole.DestructiveRole)
            cancel_btn = msg.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
            msg.exec()
            if msg.clickedButton() == save_btn:
                if not self._save_editor(widget):
                    return  # keep the tab (and its unsaved content) open
            elif msg.clickedButton() == cancel_btn:
                return
        self._discard_tab(index)
        # Saving (or discarding) the last unsaved file may unblock a
        # pending auto-reload.
        self._maybe_auto_reload()

    def _discard_tab(self, index: int):
        """Remove the tab at `index` and destroy its editor. removeTab
        alone only detaches the page, leaving the editor (with its tile
        table, undo history and so on) alive until the window closes."""
        widget = self.tab_widget.widget(index)
        self.tab_widget.removeTab(index)
        widget.deleteLater()

    def _on_tab_changed(self, index: int):
        """Update window state when tab changes."""
        self._update_cursor_pos_label()

    def _update_cursor_pos_label(self):
        editor = self._current_editor()
        if isinstance(editor, TextEditor):
            cursor = editor.textCursor()
            self.cursor_pos_label.setText(
                f"Ln {cursor.blockNumber() + 1}, "
                f"Col {cursor.columnNumber() + 1}")
        else:
            self.cursor_pos_label.setText("")

    def _current_editor(self):
        return self.tab_widget.currentWidget()

    def _save_editor(self, editor) -> bool:
        """Save one editor's file, reporting any failure to the user.
        Returns False if the save failed (the editor keeps its unsaved
        state)."""
        try:
            editor.save()
        except Exception as e:
            QMessageBox.warning(
                self, "Error saving file",
                f"Could not save "
                f"{os.path.basename(editor.filepath)}:\n\n{e}")
            return False
        self._update_tab_title(editor)
        self._note_saved(editor)
        return True

    def _save_current(self):
        editor = self._current_editor()
        if editor and hasattr(editor, 'save'):
            if self._save_editor(editor):
                self._maybe_auto_reload()

    def _save_all(self):
        for editor in self._unsaved_editors():
            if not self._save_editor(editor):
                return
        self._maybe_auto_reload()

    def _revert_current(self):
        """Revert the current tab to its on-disk content. Does nothing
        if the tab has no unsaved changes. Confirms first: reverting
        clears the undo history, so it can't be undone."""
        editor = self._current_editor()
        if not editor or not getattr(editor, 'unsaved', False):
            return
        name = os.path.basename(editor.filepath)
        reply = QMessageBox.question(
            self, "Revert File",
            f"Discard unsaved changes to {name} and reload it from "
            "disk?\n\nThis cannot be undone.")
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            editor.revert()
        except Exception as e:
            QMessageBox.warning(
                self, "Error reverting file",
                f"Could not re-read {name}:\n\n{e}")
            return
        # Discarding the last unsaved file may unblock a pending
        # auto-reload.
        self._maybe_auto_reload()

    def _on_application_state_changed(self, state):
        if state == Qt.ApplicationState.ApplicationActive:
            # Deferred: activation is delivered mid-way through the
            # platform's focus handling, and the check may open a modal
            # dialog (see deferred()).
            QTimer.singleShot(0, self._check_external_changes_deferred)

    def _check_external_changes_deferred(self):
        release_stale_mouse_grab()
        self._check_external_changes()

    def _externally_changed_editors(self) -> list:
        return [self.tab_widget.widget(i)
                for i in range(self.tab_widget.count())
                if hasattr(self.tab_widget.widget(i), 'disk_changed')
                and self.tab_widget.widget(i).disk_changed()]

    def _check_external_changes(self):
        """Offer to reload any open file that has been modified on disk by
        another program since this editor last read or wrote it.

        One dialog per changed file, with that file's tab brought to the
        front so it's clear what's being asked about. Keeping the
        editor's version accepts the on-disk state as known, so the same
        modification isn't asked about again; any further external
        write will be.
        """
        if self._checking_external_changes:
            return
        self._checking_external_changes = True
        try:
            lua_changed = False
            for editor in self._externally_changed_editors():
                if _is_lua(editor.filepath):
                    lua_changed = True
                self.tab_widget.setCurrentWidget(editor)
                if self._ask_reload_external(editor):
                    try:
                        editor.revert()
                    except Exception as e:
                        QMessageBox.warning(
                            self, "Error reloading file",
                            f"Could not re-read "
                            f"{os.path.basename(editor.filepath)}:\n\n{e}")
                        editor.mark_disk_current()
                else:
                    # Kept: the editor now differs from disk, so treat it
                    # like an unsaved edit. Closing prompts to save (else
                    # the kept version would silently be lost to the
                    # external one), Revert File offers a way to take the
                    # disk version later, and the mod auto-reload below
                    # waits until this version has been written out.
                    editor.mark_disk_current()
                    editor.mark_modified()
            # Whether reloaded into the editor or not, the mod's Lua on
            # disk differs from what was last loaded, so the tile tables
            # may be stale (same situation as after saving a .lua file).
            if lua_changed:
                self._lua_changed_since_reload = True
                self._maybe_auto_reload()
        finally:
            self._checking_external_changes = False

    def _ask_reload_external(self, editor) -> bool:
        """Ask whether to reload `editor`'s file after an external
        modification. Returns True to reload (discarding any unsaved
        changes in the editor), False to keep the editor's version."""
        name = os.path.basename(editor.filepath)
        msg = QMessageBox(self)
        msg.setWindowTitle("File Changed on Disk")
        msg.setText(f"{name} has been modified by another program.")
        msg.setIcon(QMessageBox.Icon.Question)
        if editor.unsaved:
            msg.setInformativeText(
                "This editor also has unsaved changes to it. Reloading "
                "discards those changes and cannot be undone.")
            reload_btn = msg.addButton(
                "Reload (Discard Changes)",
                QMessageBox.ButtonRole.DestructiveRole)
            keep_btn = msg.addButton(
                "Keep My Version", QMessageBox.ButtonRole.RejectRole)
            msg.setDefaultButton(keep_btn)
        else:
            msg.setInformativeText("Reload it from disk?")
            reload_btn = msg.addButton(
                "Reload", QMessageBox.ButtonRole.AcceptRole)
            msg.addButton("Keep Editor Version",
                          QMessageBox.ButtonRole.RejectRole)
            msg.setDefaultButton(reload_btn)
        msg.exec()
        return msg.clickedButton() == reload_btn

    def _undo(self):
        editor = self._current_editor()
        if editor and hasattr(editor, 'undo'):
            editor.undo()

    def _redo(self):
        editor = self._current_editor()
        if editor and hasattr(editor, 'redo'):
            editor.redo()

    def _cut(self):
        editor = self._current_editor()
        if editor and hasattr(editor, 'cut'):
            editor.cut()

    def _copy(self):
        editor = self._current_editor()
        if editor and hasattr(editor, 'copy'):
            editor.copy()

    def _paste(self):
        editor = self._current_editor()
        if editor and hasattr(editor, 'paste'):
            editor.paste()

    def _find(self):
        editor = self._current_editor()
        if editor and hasattr(editor, 'show_find_bar'):
            editor.show_find_bar()

    def _find_next(self):
        editor = self._current_editor()
        if editor and hasattr(editor, 'find_next'):
            editor.find_next()

    def _find_previous(self):
        editor = self._current_editor()
        if editor and hasattr(editor, 'find_previous'):
            editor.find_previous()

    def _goto_line(self):
        editor = self._current_editor()
        if editor and hasattr(editor, 'goto_line'):
            editor.goto_line()

    # -- Find in Files ----------------------------------------------------

    def _editor_text_for_search(self, filepath: str) -> str | None:
        """Current buffer of an open text editor for `filepath` (so a
        search sees unsaved edits), or None if it isn't open as text."""
        editor = self._find_open_tab(filepath)
        if isinstance(editor, TextEditor):
            return editor.content_text()
        return None

    def _find_in_files(self):
        """Edit > Find in Files: show the panel, seeded with the current
        editor's selection (if it's a single line)."""
        initial = None
        editor = self._current_editor()
        if isinstance(editor, TextEditor):
            sel = editor.textCursor().selectedText()
            # Qt's paragraph separator marks a multi-line selection: skip
            if sel and '\u2029' not in sel:
                initial = sel
        self.find_dock.show()
        self.find_panel.focus_query(initial)

    def _close_find_in_files(self):
        self.find_dock.hide()
        editor = self._current_editor()
        if editor is not None:
            editor.setFocus()

    def _step_search_result(self, delta: int):
        """F4 / Shift+F4: jump to the next/previous Find in Files result."""
        if not self.find_panel.has_results():
            self._show_status("No search results (use Edit > Find in Files)")
            return
        self.find_dock.show()
        self.find_panel.goto_adjacent(delta)

    def _goto_match(self, filepath: str, line: int, col: int, length: int):
        """Open (or focus) a file and select a Find in Files match."""
        editor = self._open_or_focus_file(filepath)
        if editor is None:
            self._show_status_error(
                f"Could not open {os.path.basename(filepath)}")
        elif isinstance(editor, TextEditor):
            editor.select_range(line, col, length)
        else:
            # Segments files open in the segment editor, which has no
            # notion of a text line to jump to.
            self._show_status(
                f"Match is on line {line} of "
                f"{os.path.basename(filepath)} (shown in the segment "
                "editor, so it can't be selected)")

    def _zoom_in(self):
        editor = self._current_editor()
        if editor and hasattr(editor, 'zoom_in'):
            editor.zoom_in()

    def _zoom_out(self):
        editor = self._current_editor()
        if editor and hasattr(editor, 'zoom_out'):
            editor.zoom_out()

    def _zoom_reset(self):
        editor = self._current_editor()
        if editor and hasattr(editor, 'zoom_reset'):
            editor.zoom_reset()

    def _add_segment(self):
        editor = self._current_editor()
        if isinstance(editor, SegmentEditor):
            editor.add_segment()

    def _delete_segment(self):
        editor = self._current_editor()
        if isinstance(editor, SegmentEditor):
            editor.delete_segment()

    # --- Test Segment ---

    def _resolve_knights_binary(self) -> str | None:
        """The configured Knights executable, prompting for it (via the
        Settings dialog) if it can't be found. None if the user
        cancelled or the new setting can't be found either."""
        binary = config.get_knights_binary()
        if config.knights_binary_found(binary):
            return binary
        if not self._show_settings(
                f"The Knights program '{binary}' could not be found. Please "
                "set the Knights executable so the segment can be tested."):
            return None
        binary = config.get_knights_binary()
        return binary if config.knights_binary_found(binary) else None

    def _test_segment(self):
        """Run the game on the selected segment (see segtest.py)."""
        editor = self._current_editor()
        if not isinstance(editor, SegmentEditor):
            self._show_status_error(
                "Test Segment: open a segments file and select a segment first")
            return
        index = editor.current_index()
        if index < 0:
            self._show_status_error("Test Segment: no segment is selected")
            return
        if self.mod_loader is None:
            self._show_status_error("Test Segment: no mod is loaded")
            return
        segment_name = editor.segments[index].name

        # The game reads the segments file from disk, so it must be
        # saved; this is the file being tested, so just save it. Other
        # unsaved files (Lua) are the user's call.
        if editor.unsaved:
            try:
                editor.save()
            except Exception as e:
                QMessageBox.warning(
                    self, "Error saving file",
                    f"Could not save {os.path.basename(editor.filepath)}:"
                    f"\n\n{e}\n\nThe segment was not tested.")
                return
            self._update_tab_title(editor)
            self._note_saved(editor)
        if self._any_unsaved():
            choice = self._prompt_save_all(
                "Save changes before testing the segment?",
                "The game reads files from disk, so unsaved changes will "
                "not be part of the test.",
                "Test Anyway",
                "The segment was not tested.")
            if choice == 'cancel':
                return
        self._maybe_auto_reload()

        owner = find_owning_module(editor.filepath, self.mod_loader.get_mod_dirs())
        if owner is None:
            QMessageBox.warning(
                self, "Cannot test segment",
                f"{os.path.basename(editor.filepath)} is not inside the "
                "directory of any loaded module, so the game would not "
                "be able to find it.")
            return
        module_name, relname = owner

        wall = default_wall_tile(editor.tile_table)
        if wall is None:
            QMessageBox.warning(
                self, "Cannot test segment",
                "No wall tile could be found: the tile table for this file "
                "has no entry that is blocked at walking level.")
            return

        source = self.mod_loader.fallback_tile_table_source()
        tt_module, tt_name = (source[0], source[1]) if source else ("", "tile_table")

        knights_data = self.ensure_knights_data_dir()
        if knights_data is None:
            return
        binary = self._resolve_knights_binary()
        if binary is None:
            return

        params = SegtestParams(
            module_name=module_name, segments_filename=relname,
            segment_index=index + 1, wall_tile_index=wall,
            tile_table_module=tt_module, tile_table_name=tt_name)
        temp_dir = make_temp_modules_dir()
        try:
            write_test_modules(temp_dir, params)
        except Exception as e:
            shutil.rmtree(temp_dir, ignore_errors=True)
            QMessageBox.warning(
                self, "Cannot test segment",
                f"Could not set up the test module:\n\n{e}")
            return

        # The game loads each module from the same directory the editor
        # loaded it from.
        command = build_command(binary, knights_data, temp_dir,
                                self.mod_loader.load_order,
                                self.mod_loader.get_mod_dirs())
        run = SegmentTestRun(command, temp_dir, self)
        run.finished.connect(
            lambda ok, message, run=run:
                self._on_segment_test_finished(run, segment_name, ok, message))
        self._segment_tests.append(run)
        run.start()
        self._show_status(
            f"Testing segment '{segment_name}' ({module_name}/{relname}, "
            f"segment {index + 1}) in Knights...", timeout_ms=0)

    def _on_segment_test_finished(self, run: SegmentTestRun,
                                  segment_name: str, ok: bool, message: str):
        if run in self._segment_tests:
            self._segment_tests.remove(run)
        if ok:
            self._show_status(f"Knights finished testing '{segment_name}'.")
            return
        self._show_status_error(f"Test of '{segment_name}' failed")
        dlg = QDialog(self)
        dlg.setWindowTitle("Test Segment failed")
        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel("Knights did not run normally. Output:"))
        text = QPlainTextEdit(message)
        text.setReadOnly(True)
        text.setFont(QFont("Monospace"))
        text.setMinimumSize(700, 350)
        layout.addWidget(text)
        close = QPushButton("Close")
        close.clicked.connect(dlg.accept)
        layout.addWidget(close, alignment=Qt.AlignmentFlag.AlignRight)
        dlg.exec()

    def closeEvent(self, event):
        if self._segment_tests:
            msg = QMessageBox(self)
            msg.setWindowTitle("Segment test running")
            msg.setText("Knights is still running a segment test. "
                        "Quitting will close the game.")
            msg.setIcon(QMessageBox.Icon.Warning)
            quit_btn = msg.addButton("Quit Anyway",
                                     QMessageBox.ButtonRole.DestructiveRole)
            msg.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
            msg.exec()
            if msg.clickedButton() != quit_btn:
                event.ignore()
                return
        if self._any_unsaved():
            choice = self._prompt_save_all(
                "Save changes before quitting?",
                "Unsaved changes will be lost.",
                "Discard Changes",
                "The editor was not closed.")
            if choice == 'cancel':
                event.ignore()
                return
        # Only stop the game once the close is committed, so cancelling
        # at either prompt leaves a running test untouched. Stopping
        # explicitly (rather than letting QProcess's destructor kill it)
        # also removes the temporary modules directory.
        for run in list(self._segment_tests):
            run.stop()
        self._segment_tests.clear()
        event.accept()
