"""Introspection of the loaded Lua state, for the Lua state browser.

This module knows nothing about Qt: it classifies Lua values, produces
short display summaries, enumerates children (table entries / object
properties), and finds references between objects. lua_state_widget.py
turns the results into a tree and a properties panel.
"""

import os
from dataclasses import dataclass

import lupa

from knights_editor.mod_loader import ModLoader, KtsObject, KtsGraphic, KtsSound
from knights_editor.models import Segment


# Kinds for plain Lua values. KtsObject values use their `kind` attribute
# ("Graphic", "Tile", "Anim", "ItemType", "MonsterType", "Sound",
# "Overlay", "Control", or a constant/function name) instead.
KIND_TABLE = 'table'
KIND_FUNCTION = 'function'
KIND_STRING = 'string'
KIND_NUMBER = 'number'
KIND_BOOLEAN = 'boolean'
KIND_NIL = 'nil'
KIND_BUILTIN = 'builtin'   # a Python callable installed by the editor
KIND_SEGMENTS = 'segments'  # Lua table whose values are all Segments
                            # (a kts.LoadSegments result, or a mod-built
                            # combination of such results)
KIND_SEGMENT = 'segment'
KIND_OTHER = 'other'

# The kinds produced by the main kts constructors; anything else from a
# KtsObject (action constants, results of no-op gameplay functions) is
# shown generically.
OBJECT_KINDS = ('Graphic', 'Tile', 'Anim', 'ItemType', 'MonsterType',
                'Sound', 'Overlay', 'Control')

# Order of kinds when sorting tree entries by type. Kinds not listed
# (action constants etc.) go after the listed kts objects.
_TYPE_ORDER = {kind: i for i, kind in enumerate([
    'Tile', 'Graphic', 'Anim', 'Overlay', 'ItemType', 'MonsterType',
    'Sound', 'Control',
])}
_TYPE_ORDER_DEFAULT = len(_TYPE_ORDER)
_TYPE_ORDER.update({kind: _TYPE_ORDER_DEFAULT + 1 + i for i, kind in enumerate([
    KIND_TABLE, KIND_SEGMENTS, KIND_SEGMENT, KIND_FUNCTION, KIND_STRING,
    KIND_NUMBER, KIND_BOOLEAN, KIND_BUILTIN, KIND_NIL, KIND_OTHER,
])})

# Property display order per object kind: these come first (in this
# order, if present), the rest follow alphabetically.
_PRIORITY_PROPS = {
    'Tile': ['graphic', 'open_graphic', 'depth', 'type', 'access',
             'items', 'connectivity_check', 'map_as', 'on_activate',
             'on_approach', 'on_withdraw', 'on_walk_over', 'on_hit',
             'on_open_or_close', 'on_destroy'],
    'ItemType': ['type', 'graphic', 'stack_graphic', 'backpack_graphic',
                 'backpack_overdraw', 'backpack_slot', 'overlay',
                 'max_stack', 'on_pick_up', 'on_drop', 'on_walk_over',
                 'on_hit', 'control', 'melee_damage',
                 'melee_backswing_time', 'melee_downswing_time',
                 'melee_stun_time', 'melee_tile_damage', 'parry_chance'],
    'MonsterType': ['type', 'anim', 'health', 'speed', 'corpse_tiles',
                    'weapon', 'on_attack', 'on_damage', 'on_death',
                    'on_move'],
}


@dataclass
class Node:
    """One entry in the state tree: a key in some container and its value."""
    key: object          # raw Lua key (str, int, ...), or None for roots
    label: str           # display text for the key
    kind: str            # see KIND_* / KtsObject.kind
    summary: str         # short text describing the value
    value: object        # the raw value
    expandable: bool     # whether children() may return anything


@dataclass
class Reference:
    """A place in the loaded state that refers to an object."""
    text: str            # e.g. "t_floor1.graphic" or "tile_table[66]"
    holder: object       # the global (object or table) containing the reference
    holder_label: str    # the global's display name


def kind_of(value) -> str:
    """Classify a value (Python-side representation of a Lua value)."""
    if value is None:
        return KIND_NIL
    if isinstance(value, KtsObject):
        return value.kind
    if isinstance(value, bool):
        return KIND_BOOLEAN
    if isinstance(value, (int, float)):
        return KIND_NUMBER
    if isinstance(value, (str, bytes)):
        return KIND_STRING
    if isinstance(value, Segment):
        return KIND_SEGMENT
    lt = lupa.lua_type(value)
    if lt == 'table':
        return KIND_SEGMENTS if _is_segment_table(value) else KIND_TABLE
    if lt == 'function':
        return KIND_FUNCTION
    if lt is not None:
        return KIND_OTHER
    if callable(value):
        return KIND_BUILTIN
    return KIND_OTHER


def is_lua_table(value) -> bool:
    return lupa.lua_type(value) == 'table'


def _is_segment_table(table) -> bool:
    """Whether a Lua table's values are all Segments (and there is at
    least one). Short-circuits on the first non-Segment value, so this
    is cheap for ordinary tables."""
    found = False
    try:
        for _, v in table.items():
            if not isinstance(v, Segment):
                return False
            found = True
    except Exception:
        return False
    return found


def key_label(key) -> str:
    if isinstance(key, str):
        return key
    if isinstance(key, bool):
        return f"[{'true' if key else 'false'}]"
    if isinstance(key, (int, float)):
        return f"[{key}]"
    return f"[{kind_of(key)}]"


def _join(label: str, key) -> str:
    """Append a key to a path label: a.b for string keys, a[1] otherwise."""
    if isinstance(key, str):
        return f"{label}.{key}"
    return label + key_label(key)


def _sort_key(key):
    # Integers (array part) first in numeric order, then strings
    # case-insensitively, then anything else.
    if isinstance(key, bool):
        return (2, str(key), "")
    if isinstance(key, (int, float)):
        return (0, key, "")
    if isinstance(key, str):
        return (1, key.lower(), key)
    return (3, str(type(key)), "")


def _lua_items(table, limit: int | None = None) -> list:
    """Own (key, value) pairs of a Lua table, sorted for display."""
    try:
        items = list(table.items())
    except Exception:
        return []
    items.sort(key=lambda kv: _sort_key(kv[0]))
    if limit is not None:
        items = items[:limit]
    return items


def _has_items(table) -> bool:
    try:
        for _ in table.items():
            return True
    except Exception:
        pass
    return False


def _short_string(s: str, limit: int = 48) -> str:
    s = s.replace("\n", "\\n")
    if len(s) > limit:
        s = s[:limit - 1] + "…"
    return f'"{s}"'


class LuaInspector:
    """Read-only view over a ModLoader's Lua state."""

    def __init__(self, loader: ModLoader, mod_name: str,
                 sort_by_type: bool = False):
        self.loader = loader
        self.mod_name = mod_name
        # When set, children() groups entries by kind (see _TYPE_ORDER)
        # before sorting by key.
        self.sort_by_type = sort_by_type
        self._names = loader.scan_object_names()
        # debug is stripped from the sandboxed _G, so the loader keeps
        # this helper (built while debug still existed) for us.
        self._getinfo = loader.fn_source_info
        self._rawequal = loader.lua.eval('rawequal')
        self._rawget = loader.lua.eval('rawget')

    def same_value(self, a, b) -> bool:
        """Whether two values are the same Lua object. Needed for tables
        (and functions): lupa hands out a fresh wrapper each time one is
        fetched, so Python `is`/`==` compare wrappers, not the table."""
        if a is b:
            return True
        if lupa.lua_type(a) is None or lupa.lua_type(b) is None:
            return False
        try:
            return bool(self._rawequal(a, b))
        except Exception:
            return False

    # --- naming -------------------------------------------------------

    def refresh_names(self):
        self._names = self.loader.scan_object_names()

    def global_name(self, value) -> str | None:
        """Display name of the global a stub object is bound to, e.g.
        "t_floor1" for the opened mod's own globals or "base.t_floor1"
        for a dependency's."""
        entry = self._names.get(id(value)) if isinstance(value, KtsObject) \
            else None
        if entry is None:
            return None
        mod, name = entry
        if mod == self.mod_name or mod == "_G":
            return name
        return f"{mod}.{name}"

    def global_location(self, value) -> tuple[str, str] | None:
        """(mod name, global name) the value is bound to, if any.

        Stub objects are looked up in the name map; Lua tables and
        functions (which have no stable Python identity) are found by
        scanning the namespaces with rawequal.
        """
        if isinstance(value, KtsObject):
            return self._names.get(id(value))
        if lupa.lua_type(value) not in ('table', 'function'):
            return None
        spaces = list(self.namespaces())
        spaces.append(("_G", self.globals_table()))
        for mod, ns in spaces:
            if self.same_value(ns, value):
                continue   # the namespace itself isn't a global within it
            for k, v in _lua_items(ns):
                if isinstance(k, str) and self.same_value(v, value):
                    return mod, k
        return None

    def display_path(self, path: str) -> str:
        """A file path relative to the knights_data directory where
        possible (e.g. "modules/base/gfx/floor1.bmp")."""
        root = os.path.realpath(self.loader.knights_data_dir)
        real = os.path.realpath(path)
        if real.startswith(root + os.sep):
            return os.path.relpath(real, root).replace(os.sep, "/")
        return path

    # --- summaries ----------------------------------------------------

    def summary(self, value, key=None) -> str:
        """Short description of a value for the tree's value column. If
        the value is a named global other than `key` itself, the global
        name is shown (so t_floor1's graphic shows as "g_floor1")."""
        kind = kind_of(value)
        if kind == KIND_NIL:
            return "nil"
        if kind == KIND_BOOLEAN:
            return "true" if value else "false"
        if kind == KIND_NUMBER:
            return repr(value)
        if kind == KIND_STRING:
            if isinstance(value, bytes):
                value = value.decode('utf-8', 'replace')
            return _short_string(value)
        if kind == KIND_TABLE:
            n = 0
            try:
                for _ in value.items():
                    n += 1
                    if n > 999:
                        return "{1000+ entries}"
            except Exception:
                pass
            if n == 0:
                return "{}"
            return f"{{{n} entr{'y' if n == 1 else 'ies'}}}"
        if kind == KIND_FUNCTION:
            loc = self.function_source(value)
            if loc:
                return f"function  {os.path.basename(loc[0])}:{loc[1]}"
            return "function"
        if kind == KIND_BUILTIN:
            return "function (editor stub)"
        if kind == KIND_SEGMENTS:
            n = sum(1 for _ in value.items())
            return f"{n} segment{'s' if n != 1 else ''}"
        if kind == KIND_SEGMENT:
            size = f"{value.width}\u00d7{value.height}"
            if value.name:
                return f"{value.name}  (segment {size})"
            return f"segment {size}"
        if isinstance(value, KtsObject):
            name = self.global_name(value)
            detail = self._object_detail(value)
            loc = self.global_location(value)
            if name is not None and not (loc and loc[1] == key):
                return f"{name}  ({detail})" if detail else name
            return detail or value.kind
        return kind

    def _object_detail(self, obj: KtsObject) -> str:
        kind = obj.kind
        if isinstance(obj, KtsGraphic):
            return os.path.basename(obj.raw_filename)
        if kind == 'Sound':
            return str(obj.args[0]) if obj.args else "Sound"
        if kind in ('Anim', 'Overlay'):
            n = len(obj.graphics())
            return f"{kind}, {n} frame{'s' if n != 1 else ''}"
        if kind == 'Tile':
            g = obj.props.get('graphic')
            gname = self.global_name(g) if isinstance(g, KtsGraphic) else None
            return f"Tile {gname}" if gname else "Tile"
        if kind in ('ItemType', 'MonsterType'):
            t = obj.props.get('type')
            return f"{kind} ({t})" if isinstance(t, str) else kind
        if kind == 'Control':
            a = obj.props.get('action')
            aname = self.global_name(a) if isinstance(a, KtsObject) else None
            return f"Control {aname}" if aname else "Control"
        return kind

    # --- tree ---------------------------------------------------------

    def node(self, key, value) -> Node:
        label = key_label(key) if key is not None else ""
        return Node(key=key, label=label, kind=kind_of(value),
                    summary=self.summary(value, key), value=value,
                    expandable=self.is_expandable(value))

    def is_expandable(self, value) -> bool:
        if isinstance(value, KtsObject):
            return bool(value.props) or any(
                a is not None for a in value.args)
        if is_lua_table(value):
            return _has_items(value)
        return False

    def children(self, value) -> list[Node]:
        """Child nodes of a table or object, sorted by key (or by kind
        then key when sort_by_type is set)."""
        if isinstance(value, KtsObject):
            nodes = [self.node(k, v) for k, v in value.props.items()]
            if value._lua_table is None:
                # Positional constructor (Graphic, Sound): show the args.
                for i, a in enumerate(value.args, start=1):
                    if a is not None:
                        nodes.append(self.node(i, a))
        elif is_lua_table(value):
            nodes = [self.node(k, v) for k, v in _lua_items(value)]
        else:
            return []
        nodes.sort(key=self._node_sort_key)
        return nodes

    def _node_sort_key(self, node: Node):
        if self.sort_by_type:
            rank = _TYPE_ORDER.get(node.kind, _TYPE_ORDER_DEFAULT)
        else:
            rank = 0
        return (rank, _sort_key(node.key))

    def namespaces(self) -> list[tuple[str, object]]:
        return self.loader.get_namespaces()

    def globals_table(self):
        return self.loader.lua.globals()

    # --- functions ----------------------------------------------------

    def function_source(self, fn) -> tuple[str, int] | None:
        """(file path, line) where a Lua function was defined, or None
        for builtins/editor stubs/functions without a file source."""
        if kind_of(fn) != KIND_FUNCTION:
            return None
        try:
            source, line = self._getinfo(fn)
        except Exception:
            return None
        if isinstance(source, bytes):
            source = source.decode('utf-8', 'replace')
        if not isinstance(source, str) or not source.startswith("@"):
            return None
        try:
            line = int(line)
        except (TypeError, ValueError):
            return None
        return source[1:], line

    def definition_source(self, value,
                          name: str | None = None) -> tuple[str, int] | None:
        """(file path, line) where a value is defined in the Lua code:
        the constructor call site for kts.* objects, the line a function
        was defined at, or the assignment that created the global (see
        ModLoader.global_definition). Plain values (strings, numbers)
        can't be identified by value, so `name` - the label the value is
        shown under - is used, accepting a namespace only if it holds
        this exact value under that name. None when not known."""
        if isinstance(value, KtsObject) and value.definition is not None:
            return value.definition
        loc = self.function_source(value)
        if loc is not None:
            return loc
        spaces = dict(self.namespaces())
        entry = self.global_location(value)
        if entry is not None:
            candidates = [entry]
        elif name:
            ordered = sorted(spaces, key=lambda m: m != self.mod_name)
            candidates = [(mod, name) for mod in ordered
                          if self._ns_holds(spaces[mod], name, value)]
        else:
            candidates = []
        for mod, key in candidates:
            ns = spaces.get(mod)
            if ns is None:
                continue
            site = self.loader.global_definition(ns, key)
            if site is not None:
                return site
        return None

    def _ns_holds(self, ns, name: str, value) -> bool:
        """Whether the namespace's own entry `name` (no _G fallback) is
        this exact value."""
        try:
            cur = self._rawget(ns, name)
            if cur is None:
                return False
            if cur is value or cur == value:
                return True
        except Exception:
            return False
        return self.same_value(cur, value)

    # --- properties ---------------------------------------------------

    def properties(self, value) -> list[tuple[str, object]]:
        """(label, value) rows for the properties panel. Graphics,
        sounds and other positional objects get synthesised rows; table
        objects get their props with the important ones first."""
        if isinstance(value, KtsGraphic):
            rows = [
                ("File", value.raw_filename),
                ("Path", self.display_path(value.filename)),
                ("Handle", f"({value.handle_pos[0]}, {value.handle_pos[1]})"),
                ("Colour key", value.color_key),
            ]
            if value.size_hint != (1, 1):
                rows.append(("Scale", f"{value.size_hint[0]}/{value.size_hint[1]}"))
            rows += self._prop_rows(value)
            return rows
        if isinstance(value, KtsObject):
            if value.kind == 'Sound':
                if isinstance(value, KtsSound):
                    rows = [("File", value.raw_filename),
                            ("Path", self.display_path(value.filename))]
                else:
                    rows = [("File", str(value.args[0]))] if value.args else []
                return rows + self._prop_rows(value)
            rows = []
            if value.kind in ('Anim', 'Overlay'):
                frames = value.graphics()
                rows.append(("Frames", len(frames)))
                rows += self._prop_rows(value, skip_ints=True)
                return rows
            if value._lua_table is None and value.args:
                rows += [(f"Argument {i}", a)
                         for i, a in enumerate(value.args, start=1)
                         if a is not None]
            rows += self._prop_rows(value)
            return rows
        if is_lua_table(value):
            return [(key_label(k), v) for k, v in _lua_items(value, 200)]
        if isinstance(value, Segment):
            return [("Name", value.name),
                    ("Size", f"{value.width} \u00d7 {value.height}"),
                    ("Rooms", len(value.rooms)),
                    ("Switches", len(value.switches))]
        return []

    def _prop_rows(self, obj: KtsObject, skip_ints=False):
        props = dict(obj.props)
        rows = []
        for name in _PRIORITY_PROPS.get(obj.kind, []):
            if name in props:
                rows.append((name, props.pop(name)))
        for k in sorted(props.keys(), key=_sort_key):
            if skip_ints and isinstance(k, (int, float)) \
                    and not isinstance(k, bool):
                continue
            rows.append((key_label(k), props[k]))
        return rows

    # --- references ---------------------------------------------------

    def find_references(self, target,
                        limit: int = 60) -> tuple[list[str], list[Reference]]:
        """Find where `target` appears in the loaded state.

        Returns (aliases, references): aliases are the display names of
        every global bound directly to the object (including its primary
        name); references are globals that refer to it via a property or
        a table entry up to two levels deep (enough for tile_table
        composites like {t_door, t_bg}). Only stub objects are searched
        for, since those have stable Python identity.
        """
        if not isinstance(target, KtsObject):
            return [], []
        aliases: list[str] = []
        refs: list[Reference] = []
        spaces = list(self.namespaces())
        spaces.append(("_G", self.globals_table()))
        seen_ns = set()
        for mod, ns in spaces:
            if id(ns) in seen_ns:
                continue
            seen_ns.add(id(ns))
            prefix = "" if mod in (self.mod_name, "_G") else mod + "."
            for k, v in _lua_items(ns):
                if not isinstance(k, str):
                    continue
                label = prefix + k
                if v is target:
                    aliases.append(label)
                    continue
                if isinstance(v, KtsObject):
                    for pk, pv in v.props.items():
                        if pv is target:
                            refs.append(Reference(
                                _join(label, pk), v, label))
                        elif is_lua_table(pv):
                            for ik, iv in _lua_items(pv, 64):
                                if iv is target:
                                    refs.append(Reference(
                                        _join(_join(label, pk), ik),
                                        v, label))
                elif is_lua_table(v) and v is not ns:
                    for tk, tv in _lua_items(v):
                        if tv is target:
                            refs.append(Reference(
                                _join(label, tk), v, label))
                        elif is_lua_table(tv):
                            for ik, iv in _lua_items(tv, 16):
                                if iv is target:
                                    refs.append(Reference(
                                        _join(_join(label, tk), ik),
                                        v, label))
                if len(refs) >= limit:
                    return aliases, refs
        return aliases, refs
