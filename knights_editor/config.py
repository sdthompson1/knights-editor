"""Persistent editor preferences, stored via QSettings."""

import json
import os
import shutil
from PySide6.QtCore import QSettings


def _settings() -> QSettings:
    return QSettings("knights-editor", "knights-editor")


def _read_str_list(key: str) -> list[str]:
    value = _settings().value(key, [])
    # QSettings gives back a str for a one-element list and None/"" for
    # an empty one.
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value if str(v)]


def get_knights_data_dir() -> str | None:
    """The configured knights_data directory, or None if not set."""
    value = _settings().value("knights_data_dir", "")
    return str(value) if value else None


def set_knights_data_dir(path: str):
    _settings().setValue("knights_data_dir", path)


def get_knights_binary() -> str:
    """The Knights executable used by Test Segment: a full path, or a
    bare command name to be looked up on PATH (default "knights")."""
    value = _settings().value("knights_binary", "")
    return str(value) if value else "knights"


def set_knights_binary(path: str):
    _settings().setValue("knights_binary", path)


def knights_binary_found(binary: str) -> bool:
    """Whether a Knights executable setting (see get_knights_binary)
    resolves to an executable file."""
    if os.path.dirname(binary):
        return os.path.isfile(binary) and os.access(binary, os.X_OK)
    return shutil.which(binary) is not None


def get_extra_modules_dirs() -> list[str]:
    """Additional modules directories, in search order. Each is a
    directory containing mod subfolders (like knights_data/modules) and
    is searched before knights_data/modules when a mod is looked up by
    name. Test Segment passes the directory each mod was found in to
    the game as a --module-dir option."""
    return _read_str_list("extra_modules_dirs")


def set_extra_modules_dirs(dirs: list[str]):
    _settings().setValue("extra_modules_dirs", [str(d) for d in dirs])


# Recently opened mods and files (File > Open Recent), most recent first.

MAX_RECENT = 10


def _push_recent(key: str, path: str):
    path = os.path.abspath(path)
    norm = os.path.normcase(path)
    items = [p for p in _read_str_list(key) if os.path.normcase(p) != norm]
    items.insert(0, path)
    _settings().setValue(key, items[:MAX_RECENT])


def _remove_recent(key: str, path: str):
    norm = os.path.normcase(os.path.abspath(path))
    items = [p for p in _read_str_list(key) if os.path.normcase(p) != norm]
    _settings().setValue(key, items)


def get_recent_mods() -> list[str]:
    """Recently opened mod directories, most recent first."""
    return _read_str_list("recent_mods")


def add_recent_mod(mod_dir: str):
    _push_recent("recent_mods", mod_dir)


def remove_recent_mod(mod_dir: str):
    _remove_recent("recent_mods", mod_dir)


def get_recent_files() -> list[str]:
    """Files recently opened via File > Open, most recent first."""
    return _read_str_list("recent_files")


def add_recent_file(path: str):
    _push_recent("recent_files", path)


def remove_recent_file(path: str):
    _remove_recent("recent_files", path)


def clear_recent():
    s = _settings()
    s.remove("recent_mods")
    s.remove("recent_files")


def get_show_room_problems() -> bool:
    """Whether the Room Check panel's "Show on map" box is ticked."""
    value = _settings().value("show_room_problems", True)
    # QSettings may hand back a bool or the string "true"/"false".
    return value in (True, "true", "True", 1, "1")


def set_show_room_problems(show: bool):
    _settings().setValue("show_room_problems", bool(show))


# Module load orders, one per mod directory. Kept in the editor's own
# settings (rather than in the mod directory) because the load order is
# the editor user's testing configuration, not a property of the mod.
# Stored as a settings array of {path, order} entries; `order` is a JSON
# list of mod names. (Names may contain spaces: the edited mod is named
# after its directory, which can be anything the filesystem allows.)

_LOAD_ORDER_ARRAY = "mod_load_orders"


def _mod_key(mod_dir: str) -> str:
    return os.path.realpath(mod_dir)


def _decode_order(raw) -> list[str]:
    text = str(raw)
    try:
        value = json.loads(text)
    except ValueError:
        value = None
    if isinstance(value, list):
        return [str(name) for name in value]
    # Pre-JSON settings stored the names space-separated.
    return text.split()


def _read_load_orders() -> dict[str, list[str]]:
    s = _settings()
    result = {}
    count = s.beginReadArray(_LOAD_ORDER_ARRAY)
    for i in range(count):
        s.setArrayIndex(i)
        path = s.value("path", "")
        order = s.value("order", "")
        if path:
            result[str(path)] = _decode_order(order)
    s.endArray()
    return result


def _write_load_orders(orders: dict[str, list[str]]):
    s = _settings()
    s.remove(_LOAD_ORDER_ARRAY)
    s.beginWriteArray(_LOAD_ORDER_ARRAY, len(orders))
    for i, (path, order) in enumerate(orders.items()):
        s.setArrayIndex(i)
        s.setValue("path", path)
        s.setValue("order", json.dumps([str(name) for name in order]))
    s.endArray()


def get_mod_load_order(mod_dir: str) -> list[str] | None:
    """The user's explicit module load order for the mod in `mod_dir`,
    or None if the order should be inferred automatically."""
    return _read_load_orders().get(_mod_key(mod_dir))


def set_mod_load_order(mod_dir: str, order: list[str] | None):
    """Store an explicit load order for the mod in `mod_dir`, or clear it
    (order=None) to go back to automatic inference."""
    orders = _read_load_orders()
    key = _mod_key(mod_dir)
    if order is None:
        orders.pop(key, None)
    else:
        orders[key] = list(order)
    _write_load_orders(orders)
