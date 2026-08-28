"""Load Knights mod Lua files with stubbed kts.* functions.

Executes mod Lua to extract tile definitions and tile_table mapping,
so the editor knows what graphics to display for each tile number.
"""

import os
import re
from dataclasses import dataclass, field
import lupa
from lupa import LuaRuntime
from knights_editor.models import Graphic, TileInfo, TileEntry
from knights_editor.segment_io import load_segments as load_segments_from_file
from knights_editor.text_files import read_bytes_as_text


# Matches "<path>.lua:<line>:" as Lua puts at the front of error messages
# (chunks are loaded with "@<path>" chunknames, see ModLoader). The path
# may not contain ':' except for a Windows drive letter, which is what
# stops this matching the wrapper's own '[string "<python>"]:1:' prefix.
_LUA_LOCATION_RE = re.compile(r'((?:[A-Za-z]:)?[^:\n]+?\.lua):(\d+):')


def _resolve_location_path(candidate: str, known_files) -> str | None:
    """Turn the path part of a Lua error location into a real file path.

    Lua abbreviates chunknames longer than LUA_IDSIZE (60 chars) in error
    messages by keeping only the tail, prefixed with "..." - so a typical
    absolute mod path comes out as ".../modules/tutorial/init.lua". Such
    a tail is matched against the end of each path in `known_files` (the
    files the loader has read, most recent first, since the error is
    usually in the file currently being loaded).
    """
    if candidate.startswith("..."):
        tail = candidate[3:]
        if not tail:
            return None
        for path in reversed(list(known_files)):
            if path.endswith(tail) and os.path.isfile(path):
                return path
        return None
    if os.path.isfile(candidate):
        return candidate
    return None


def parse_lua_error(exc: BaseException, known_files=()):
    """Extract (filepath, line, message) from a Lua load error.

    filepath/line identify the mod file the error was reported in (the
    first location in the message that names a file that exists), or
    are None if the error carries no usable location (e.g. a Python-side
    error such as a missing module). message is the first line of the
    error with any '[string "<python>"]:N:' wrapper prefix removed, and
    the stack traceback dropped.

    `known_files` (e.g. ModLoader.loaded_files) is used to recover full
    paths that Lua abbreviated with a "..." prefix; without it such
    locations can't be resolved and filepath/line are None.
    """
    text = str(exc).strip()
    if not text:
        # e.g. Lua error() with no value
        text = f"{type(exc).__name__} with no error message"
    first = text.splitlines()[0]
    # Drop location prefixes that refer to the loader's own wrapper
    # chunks rather than mod files.
    first = re.sub(r'^(\[string "[^"]*"\]:\d+:\s*)+', '', first).strip()

    filepath = line = None
    for m in _LUA_LOCATION_RE.finditer(first):
        resolved = _resolve_location_path(m.group(1).strip(), known_files)
        if resolved is not None:
            filepath = resolved
            line = int(m.group(2))
            break
    return filepath, line, first or text


class LoadSegmentsCall:
    """Records one kts.LoadSegments call made while loading the mods."""

    def __init__(self, source: str | None, line: int | None, tile_table):
        self.source = source          # abs path of the calling Lua file
        self.line = line              # 1-based line number of the call
        self.tile_table = tile_table  # the raw Lua tile table argument

    def tile_table_expr(self) -> str | None:
        """Source text of the tile-table argument (e.g. "tile_table" or
        "std.tile_table"), or None if it can't be recovered."""
        if not self.source or not self.line:
            return None
        try:
            with open(self.source, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            text = lines[self.line - 1]
        except Exception:
            return None
        m = re.search(r'kts\.LoadSegments\s*\(\s*([^,]+?)\s*,', text)
        return m.group(1) if m else None


def _is_lua_table(value) -> bool:
    return lupa.lua_type(value) == 'table'


def _lua_attribute_filter(obj, attr_name, is_setting):
    """lupa attribute filter: which attributes of a Python object mod Lua
    may read or write. Mods legitimately use the stubs' Lua-facing
    properties and methods (B.t_floor1.access, item:get_graphic(), ...);
    anything starting with an underscore is Python internals, through
    which the interpreter is reachable (e.g. obj.__init__.__globals__
    gives the module globals, and hence os), so it is refused. This is
    enforced at the lupa boundary rather than in KtsObject.__getattr__,
    which only sees names the object doesn't really have."""
    if isinstance(attr_name, str) and attr_name.startswith('_'):
        raise AttributeError(attr_name)
    return attr_name


# MapDirection order, as in the game (shared/map_support.hpp).
_DIR_INDEX = {'north': 0, 'east': 1, 'south': 2, 'west': 3}


def parse_overlay_offsets(args):
    """Parse kts.SetOverlayOffsets arguments: triples of (direction name,
    x offset, y offset), frame-major with facing (N,E,S,W) as the fast
    index, offsets in 1/1000ths of a square. The game expects 5 frames x
    4 facings = 20 triples; any whole number of triples is accepted here.
    Returns a list of (graphic direction index, x, y) indexed by
    frame*4 + facing, or None if the arguments don't parse."""
    if not args or len(args) % 3 != 0:
        return None
    out = []
    for i in range(0, len(args), 3):
        d = args[i]
        if isinstance(d, bytes):
            d = d.decode('utf-8', 'replace')
        dir_idx = _DIR_INDEX.get(str(d).lower())
        if dir_idx is None:
            return None
        try:
            out.append((dir_idx, int(args[i + 1]), int(args[i + 2])))
        except (TypeError, ValueError):
            return None
    return out


class KtsObject:
    """Base class for stubs standing in for kts.* userdata objects
    (Graphic, Tile, Anim, ItemType, MonsterType, Sound, Overlay, Control,
    and the various action/predicate constants).

    Everything the Lua code passed to the constructor is retained so the
    editor can inspect it later:

      kind   - the kts constructor name, e.g. "Tile" or "Sound"
      args   - the positional constructor arguments exactly as passed
               (Lua tables are kept as Lua tables)
      props  - dict snapshot of the props table for table-style
               constructors (kts.Tile{...}, kts.ItemType{...}); attribute
               reads from Lua (t_switch_up.access) come from here, and
               attribute writes (B.i_hammer.on_pick_up = f) go here.

    `.table` returns the props table the object was created from (the
    same Lua table every time, so identity comparisons such as
    `v.table == B.t_chest_north.table` work), creating an empty one on
    demand for objects that weren't built from a table.
    """

    def __init__(self, kind: str, args=(), lua_runtime=None):
        self._kind = kind
        self._args = tuple(args)
        self._lua_runtime = lua_runtime
        self._lua_table = None
        self._definition = None   # (abs Lua file path, 1-based line) or None
        self._props = {}
        # Table-style constructor: a single Lua table argument.
        if len(args) == 1 and _is_lua_table(args[0]):
            self._lua_table = args[0]
            try:
                for k, v in args[0].items():
                    self._props[k] = v
            except Exception:
                pass

    @property
    def kind(self) -> str:
        return self._kind

    @property
    def definition(self) -> tuple[str, int] | None:
        """(file path, line) of the kts.* constructor call that created
        this object, when it was created from Lua."""
        return self._definition

    @property
    def args(self) -> tuple:
        return self._args

    @property
    def props(self) -> dict:
        return self._props

    @property
    def table(self):
        if self._lua_table is None:
            if self._lua_runtime is None:
                return {}
            self._lua_table = self._lua_runtime.table()
            for k, v in self._props.items():
                try:
                    self._lua_table[k] = v
                except Exception:
                    pass
        return self._lua_table

    def graphics(self) -> list['KtsGraphic']:
        """The graphics passed positionally, in order: either directly as
        arguments or as the array part of a single table argument (as in
        kts.Anim { g_1, g_2, ... } and kts.Overlay { g_n, g_e, g_s, g_w })."""
        if self._lua_table is not None:
            result = []
            i = 1
            while True:
                v = self._props.get(i)
                if v is None:
                    break
                if isinstance(v, KtsGraphic):
                    result.append(v)
                i += 1
            return result
        return [a for a in self._args if isinstance(a, KtsGraphic)]

    def __getattr__(self, name):
        if name.startswith('_'):
            raise AttributeError(name)
        return self._props.get(name)

    def __setattr__(self, name, value):
        if name.startswith('_'):
            super().__setattr__(name, value)
        else:
            self._props[name] = value
            if self._lua_table is not None:
                try:
                    self._lua_table[name] = value
                except Exception:
                    pass

    def __repr__(self):
        return f"<Kts{self._kind}>"


class KtsGraphic(KtsObject):
    """Stub for kts.Graphic() return value.

    `resolved_path` is the file `filename` refers to (see
    ModLoader.resolve_path); it is what the editor reads.
    """
    def __init__(self, filename, r=None, g=None, b=None, ox=0, oy=0,
                 size_num=1, size_denom=1,
                 resolved_path=None, lua_runtime=None):
        super().__init__("Graphic",
                         (filename, r, g, b, ox, oy, size_num, size_denom),
                         lua_runtime)
        self._filename = resolved_path if resolved_path is not None else filename
        self._raw_filename = filename
        if r is not None:
            self._color_key = (int(r), int(g), int(b))
        else:
            self._color_key = None
        self._handle_pos = (int(ox), int(oy))
        # The game's (experimental, unused by the shipped data) display
        # scale hint size_num/size_denom. Kept for the inspector; the
        # editor draws graphics unscaled.
        self._size_hint = (int(size_num), int(size_denom))

    @property
    def filename(self) -> str:
        return self._filename

    @property
    def raw_filename(self) -> str:
        """The filename as written in the Lua (relative to the module)."""
        return self._raw_filename

    @property
    def color_key(self):
        return self._color_key

    @property
    def handle_pos(self):
        return self._handle_pos

    @property
    def size_hint(self) -> tuple[int, int]:
        """(size_num, size_denom); (1, 1) when not given."""
        return self._size_hint

    def to_model(self):
        return Graphic(self._filename, self._color_key, self._handle_pos)


class KtsSound(KtsObject):
    """Stub for kts.Sound() return value.

    `resolved_path` is the file `filename` refers to (see
    ModLoader.resolve_path), as for Graphic.
    """
    def __init__(self, filename, *extra, resolved_path=None, lua_runtime=None):
        super().__init__("Sound", (filename,) + extra, lua_runtime)
        self._filename = resolved_path if resolved_path is not None else filename
        self._raw_filename = filename

    @property
    def filename(self) -> str:
        return self._filename

    @property
    def raw_filename(self) -> str:
        """The filename as written in the Lua (relative to the module)."""
        return self._raw_filename


class KtsTile(KtsObject):
    """Stub for kts.Tile() return value."""
    def __init__(self, props=None, lua_runtime=None):
        super().__init__("Tile", (props,) if props is not None else (),
                         lua_runtime)

    def get_graphic(self):
        g = self._props.get('graphic')
        if isinstance(g, KtsGraphic):
            return g.to_model()
        return None

    def get_open_graphic(self):
        g = self._props.get('open_graphic')
        if isinstance(g, KtsGraphic):
            return g.to_model()
        return None

    def get_depth(self):
        d = self._props.get('depth')
        return int(d) if d is not None else 0

    def get_walking(self) -> str | None:
        """The 'walking' entry of the tile's access table, if any."""
        acc = self._props.get('access')
        if acc is None or not _is_lua_table(acc):
            return None
        w = acc['walking']
        return str(w) if w is not None else None

    def is_destructible(self) -> bool:
        """Whether the tile has hit points (tile.cpp: hit_points <= 0, the
        default, means indestructible). `hit_points` may be a number, or
        a function/table giving a random value (always destructible).

        The engine also requires the tile to be "targettable" (not clear
        at walking height, or with an on_hit action). That only excludes
        walking-clear tiles, which are standable anyway, so it makes no
        difference to entry_standable and is ignored here."""
        hp = self._props.get('hit_points')
        if hp is None:
            return False
        if isinstance(hp, (int, float)):
            return hp > 0
        return True

    def is_door(self) -> bool:
        return self._props.get('type') == 'door'

    def get_connectivity_check(self) -> int:
        cc = self._props.get('connectivity_check')
        return int(cc) if isinstance(cc, (int, float)) else 0

    def to_tile_info(self, name: str | None = None):
        return TileInfo(
            graphic=self.get_graphic(),
            open_graphic=self.get_open_graphic(),
            depth=self.get_depth(),
            name=name,
            walking=self.get_walking(),
            destructible=self.is_destructible(),
            is_door=self.is_door(),
            connectivity_check=self.get_connectivity_check(),
        )


class KtsAnim(KtsObject):
    """Stub for kts.Anim() return value."""
    def __init__(self, *args, lua_runtime=None):
        super().__init__("Anim", args, lua_runtime)

    @property
    def first_graphic(self):
        gfx = self.graphics()
        return gfx[0] if gfx else None


class KtsItem(KtsObject):
    """Stub for kts.ItemType() return value."""
    def __init__(self, props=None, lua_runtime=None):
        super().__init__("ItemType", (props,) if props is not None else (),
                         lua_runtime)

    def get_graphic(self):
        g = self._props.get('graphic')
        if isinstance(g, KtsGraphic):
            return g.to_model()
        return None

    def get_display_graphic(self, count=1):
        """Graphic for a pile of `count` items: stack_graphic when count > 1
        (if set), otherwise the ordinary graphic."""
        if count > 1:
            g = self._props.get('stack_graphic')
            if isinstance(g, KtsGraphic):
                return g.to_model()
        return self.get_graphic()


class KtsMonster(KtsObject):
    """Stub for kts.MonsterType() return value."""
    def __init__(self, props=None, lua_runtime=None):
        super().__init__("MonsterType",
                         (props,) if props is not None else (), lua_runtime)

    def get_graphic(self):
        anim = self._props.get('anim')
        if isinstance(anim, KtsAnim) and anim.first_graphic:
            return anim.first_graphic.to_model()
        return None


def is_valid_module_name(name: str) -> bool:
    """Whether `name` is a module name the game would accept (see the
    game's IsValidModuleName): a letter followed by letters, digits or
    underscores. Directories with other names are ignored by the game."""
    if not name or not name[0].isalpha() or not name.isascii():
        return False
    return all(c.isalnum() or c == '_' for c in name[1:])


def modules_search_path(knights_data_dir: str, extra_modules_dirs=(),
                        mod_dir: str | None = None) -> list[str]:
    """The directories searched, in order, when a mod is looked up by
    name:

    - the directory containing the mod being edited (`mod_dir`), if
      any: a mod opened from ~/dev/foo most likely has its sibling
      mods in ~/dev too, so they are available without configuring
      anything;
    - the configured additional modules directories, in order;
    - <knights_data>/modules, always last (so editing an installed mod
      searches exactly as the game does with --modules-dir).

    Duplicates are dropped, keeping the earliest position."""
    installed = os.path.join(knights_data_dir, "modules")
    candidates = []
    if mod_dir is not None:
        candidates.append(os.path.dirname(os.path.abspath(mod_dir)))
    candidates += list(extra_modules_dirs)
    result, seen = [], {os.path.normcase(os.path.realpath(installed))}
    for d in candidates:
        key = os.path.normcase(os.path.realpath(d))
        if key not in seen:
            seen.add(key)
            result.append(d)
    return result + [installed]


def game_modules_dirs(knights_data_dir: str, extra_modules_dirs=(),
                      mod_dir: str | None = None) -> list[str]:
    """The --modules-dir options for a game run: the modules search
    path (see modules_search_path) without <knights_data>/modules,
    which the game adds itself, after them, from its -d option."""
    return modules_search_path(knights_data_dir, extra_modules_dirs, mod_dir)[:-1]


def find_module_dir(name: str, modules_dirs: list[str]) -> str | None:
    """The directory of the mod called `name`: the first directory in
    `modules_dirs` with a subfolder of that name containing an init.lua,
    or None if there is none."""
    for modules_dir in modules_dirs:
        mod_dir = os.path.join(modules_dir, name)
        if os.path.isfile(os.path.join(mod_dir, "init.lua")):
            return mod_dir
    return None


def list_installed_modules(knights_data_dir: str, extra_modules_dirs=(),
                           mod_dir: str | None = None) -> list[str]:
    """Names of the mods installed in any modules directory (directories
    with an init.lua and a valid module name), sorted. The arguments
    are as for modules_search_path."""
    names = set()
    for modules_dir in modules_search_path(knights_data_dir, extra_modules_dirs,
                                           mod_dir):
        try:
            entries = os.listdir(modules_dir)
        except OSError:
            continue
        names.update(
            name for name in entries
            if is_valid_module_name(name)
            and os.path.isfile(os.path.join(modules_dir, name, "init.lua")))
    return sorted(names)


def _parse_version(s: str) -> list[int] | None:
    """Dotted-integer version string -> components, or None if malformed
    (as in the game's ParseVersion)."""
    if not s:
        return None
    parts = s.split(".")
    if not all(p.isdigit() and p.isascii() for p in parts):
        return None
    return [int(p) for p in parts]


def _lua_number_to_string(value: int | float) -> str:
    """A Lua number as lua_tostring would render it: integers plainly,
    floats with "%.14g" and a ".0" added if that looks like an integer
    (so 1.0 -> "1.0", 1.5 -> "1.5")."""
    if isinstance(value, int):
        return str(value)
    s = f"{value:.14g}"
    if all(c in "-0123456789" for c in s):
        s += ".0"
    return s


def version_ge(a: str, b: str) -> bool:
    """Whether version string a >= b, comparing component-wise with
    missing components treated as 0 (the game's VersionGe). False if
    either string is malformed."""
    va, vb = _parse_version(a), _parse_version(b)
    if va is None or vb is None:
        return False
    n = max(len(va), len(vb))
    va += [0] * (n - len(va))
    vb += [0] * (n - len(vb))
    return va >= vb


@dataclass
class Discovery:
    """Result of discover_load_order(): the load order inferred by
    loading a mod with on-demand dependency loading, plus the
    dependency edges observed while doing so."""
    # Mod names in the order their init.lua finished loading. This is
    # the inferred load order: dependencies come before dependents.
    order: list[str]
    # mod name -> names it passed to require(), in call order
    required: dict[str, list[str]]
    # mod name -> names it passed to mod.GetRegisteredMod(), in call order
    optional: dict[str, list[str]]
    # The exception that stopped the load, if any. `order` is then
    # partial (mods that finished before the error).
    error: Exception | None = None
    # Files read before the error (for parse_lua_error)
    loaded_files: list[str] = field(default_factory=list)
    # Installed optional dependencies whose own loading failed: mod name
    # -> the exception. They were treated as absent and left out of
    # `order` (see ModLoader.failed_optional).
    failed_optional: dict[str, Exception] = field(default_factory=dict)


def discover_load_order(knights_data_dir: str, mod_dir: str,
                        extra_modules_dirs=()) -> Discovery:
    """Infer a load order for the mod in `mod_dir`.

    Loads the mod with dependencies loaded on demand (each require()
    call, and each mod.IsModRegistered() / GetModVersion() /
    GetModPrefix() / GetRegisteredMod() query, pulls the named mod in
    from the modules search path if it isn't loaded yet). The resulting
    completion order has every dependency before its dependents and the
    mod itself last, which is a load order the game would accept. The
    Lua state built during this pass is discarded; it is not
    game-accurate, since a dependency runs part-way through the mod
    that required it.
    """
    loader = ModLoader(knights_data_dir, on_demand=True,
                       extra_modules_dirs=extra_modules_dirs, mod_dir=mod_dir)
    error = None
    try:
        loader.load_mod_dir(mod_dir)
    except Exception as e:
        error = e
    mod_name = os.path.basename(os.path.normpath(mod_dir))
    order = list(loader.load_order)
    if error is not None and loader.error_stack:
        # The mods that were mid-load when the error hit never finished,
        # but they still belong in the order: innermost (the one that
        # actually failed) first, so that a sequential load hits the
        # same error rather than a misleading "not registered".
        for name in reversed(loader.error_stack):
            if name not in order:
                order.append(name)
    if mod_name not in order:
        order.append(mod_name)
    return Discovery(order, dict(loader.required_mods),
                     dict(loader.optional_mods), error,
                     list(loader.loaded_files),
                     dict(loader.failed_optional))


class ModLoader:
    """Loads Knights mods by executing their Lua with stubbed kts.* functions.

    Two loading modes:

    - on_demand=False (the default; matches the game): mods are loaded
      one at a time, in a caller-supplied order (see load_sequence).
      require() only returns mods that have already finished loading,
      and errors otherwise; mod.GetRegisteredMod() returns nil for
      mods not (yet) loaded.
    - on_demand=True: require() and mod.GetRegisteredMod() load the
      named mod from the modules search path if it isn't loaded yet.
      Used by discover_load_order() to infer a load order.

    Mods are looked up by name in `extra_modules_dirs` (in order) and
    then <knights_data>/modules, the first match winning - the same
    search the game does with --modules-dir options.
    """

    def __init__(self, knights_data_dir: str, on_demand: bool = False,
                 extra_modules_dirs=(), mod_dir: str | None = None):
        """`extra_modules_dirs` and `mod_dir` (the directory of the mod
        being edited, whose parent is searched first) determine where
        mods are looked up by name; see modules_search_path."""
        self.knights_data_dir = knights_data_dir
        self.modules_dirs = modules_search_path(knights_data_dir,
                                               extra_modules_dirs, mod_dir)
        self.on_demand = on_demand
        # Mods must not be able to reach the Python interpreter:
        # register_eval/register_builtins off so lupa's "python" global
        # is inert (and _apply_sandbox_whitelist removes it entirely),
        # and attribute_filter so the Python objects mods are handed
        # (the Kts* stubs, callbacks) don't leak it through their
        # internals (see _lua_attribute_filter).
        self.lua = LuaRuntime(unpack_returned_tuples=True,
                              register_eval=False,
                              register_builtins=False,
                              attribute_filter=_lua_attribute_filter)
        # The game's mod registry (_MOD_REGISTRY): the name a mod declared
        # via mod.RegisterMod -> its namespace table. This is what the
        # mod.* API and require() consult. A mod's declared name need
        # not match its directory name (mods may be stored under e.g. ID
        # numbers); the editor-side maps below are keyed by directory
        # name, which is what load orders and modules.txt use.
        self._registered_mods = {}
        self._mod_versions = {}  # declared name -> version string from RegisterMod
        self._mod_prefixes = {}  # declared name -> mod dir with trailing separator
        # dir name -> names it declared via RegisterMod, so a mod whose
        # init.lua then fails can be removed from the registry again.
        self._declared_by: dict[str, list[str]] = {}
        # On-demand mode only: installed optional dependencies whose
        # loading failed, with the exception. Treated as absent
        # thereafter (see the optional_lookup stub).
        self.failed_optional: dict[str, Exception] = {}
        self._loading = set()  # names of modules currently being loaded (cycle guard)
        # Names of the mods that were part-way through loading when the
        # first load error occurred, outermost first (see load_mod_dir).
        self.error_stack: list[str] | None = None
        self._loading_stack: list[str] = []
        self.load_order = []  # dir names in the order their init.lua finished loading
        # Set by the caller of load_sequence: whether the order it passed
        # was inferred (discover_load_order) rather than chosen by the user.
        self.load_order_auto = True
        self._mod_dirs = {}  # dir name -> directory it was loaded from
        # dir name -> namespace table; absent for mods that never called
        # mod.RegisterMod (which therefore have no globals of their own).
        self._mod_namespaces = {}
        # Dependency edges seen while loading: mod name -> names passed
        # to require() / mod.GetRegisteredMod() from that mod's code.
        self.required_mods: dict[str, list[str]] = {}
        self.optional_mods: dict[str, list[str]] = {}
        self._current_mod_dir = None
        # Directory of the Lua file currently executing: the mod dir for
        # init.lua, or a dofile'd file's own directory while it runs
        # (see resolve_path). _dir_stack holds the enclosing files'
        # directories while nested dofile calls run.
        self._current_dir = None
        self._dir_stack: list[str] = []
        self._current_mod_name = None
        self._current_env = None  # namespace of the mod currently being loaded
        self._tile_tables = {}  # mod_name -> tile_table
        self._file_tile_tables = {}  # segment file realpath -> raw Lua tile table
        self._load_segments_calls: list[LoadSegmentsCall] = []
        # Overlay offsets from the last successful kts.SetOverlayOffsets
        # call (see parse_overlay_offsets), used by the anim preview.
        self.overlay_offsets: list[tuple[int, int, int]] | None = None
        # Every Lua file read, in load order. Lets parse_lua_error map
        # the abbreviated "...<tail>" paths Lua puts in error messages
        # back to real files.
        self.loaded_files: list[str] = []
        self._setup_lua()

    def _setup_lua(self):
        """Set up the Lua environment with stubbed kts.* and mod.* functions."""
        g = self.lua.globals()

        # Create kts table
        kts = self.lua.table()
        g.kts = kts

        # kts.VERSION - KNIGHTS_VERSION_NUM from the game's
        # src/misc/version.hpp, so mods can do load-time version checks.
        kts.VERSION = 29

        lua = self.lua
        loader = self

        # Object constructors are wrapped so the Lua call site (from
        # debug.getinfo, as for kts.LoadSegments below) is recorded on
        # the created object as its definition location.
        #
        # Here and in the other helper chunks below, stdlib functions
        # that the sandbox removes from _G (debug.*, load) are captured
        # as locals while they still exist; the wrappers keep working
        # after _apply_sandbox_whitelist runs, but mods can't reach them.
        make_tracked = self.lua.eval('''
            (function()
              local getinfo = debug.getinfo
              return function(py_ctor)
                return function(...)
                  local info = getinfo(2, "Sl")
                  return py_ctor(info and info.source, info and info.currentline,
                                 ...)
                end
              end
            end)()
        ''')

        def tracked(ctor):
            def with_site(source, line, *args):
                obj = ctor(*args)
                if isinstance(source, bytes):
                    source = source.decode('utf-8', 'replace')
                if isinstance(obj, KtsObject) and isinstance(source, str) \
                        and source.startswith("@") and line:
                    obj._definition = (source[1:], int(line))
                return obj
            return make_tracked(with_site)

        # kts.Graphic - filename resolved as for any file a mod names
        # (see resolve_path)
        # Like the game's CreateGraphicFromLua, this takes 1, 4, 6 or 8
        # arguments: filename; colour key r, g, b; handle x, y; scale
        # hint size_num, size_denom.
        def kts_graphic(filename, r=None, g=None, b=None, ox=0, oy=0,
                        size_num=1, size_denom=1):
            return KtsGraphic(filename, r, g, b, ox, oy, size_num, size_denom,
                              resolved_path=loader.resolve_path(filename),
                              lua_runtime=lua)
        kts.Graphic = tracked(kts_graphic)

        # kts.Tile
        def kts_tile(props=None):
            return KtsTile(props, lua)
        kts.Tile = tracked(kts_tile)

        # kts.LoadSegments - parse segment files, and remember which tile
        # table goes with which segment file (each file's tile numbers are
        # defined by the table passed here, not by any single global table).
        # Each call site is also recorded (source file and line, from
        # debug.getinfo) so the editor can suggest a tile table and a Lua
        # snippet when creating new segment files.
        # Like the real game, the result is a plain Lua array-table of
        # segments, so mods can concatenate or subset the results with
        # ordinary table operations (#, ipairs, table.insert, ...).
        def py_load_segments(tile_table, filename, source=None, line=None):
            filepath = loader.resolve_path(filename)
            loader._file_tile_tables[os.path.realpath(filepath)] = tile_table
            src = None
            if isinstance(source, str) and source.startswith("@"):
                src = source[1:]
            loader._load_segments_calls.append(
                LoadSegmentsCall(src, int(line) if line else None, tile_table))
            return lua.table(*load_segments_from_file(filepath))

        make_load_segments = self.lua.eval('''
            (function()
              local getinfo = debug.getinfo
              return function(py_load)
                return function(tile_table, filename)
                  local info = getinfo(2, "Sl")
                  return py_load(tile_table, filename,
                                 info and info.source, info and info.currentline)
                end
              end
            end)()
        ''')
        kts.LoadSegments = make_load_segments(py_load_segments)

        # Other object constructors. Everything passed in is retained on
        # the stub (see KtsObject) so the Lua state inspector can show it.
        kts.Anim = tracked(lambda *args: KtsAnim(*args, lua_runtime=lua))
        kts.ItemType = tracked(lambda props=None: KtsItem(props, lua))
        kts.MonsterType = tracked(lambda props=None: KtsMonster(props, lua))
        # kts.Sound - filename resolved as for kts.Graphic
        def kts_sound(filename, *extra):
            return KtsSound(filename, *extra,
                            resolved_path=loader.resolve_path(filename),
                            lua_runtime=lua)
        kts.Sound = tracked(kts_sound)

        for name in ['Overlay', 'Control']:
            setattr(kts, name,
                    tracked(lambda *args, _k=name: KtsObject(_k, args, lua)))

        # No-op stubs
        kts.SetRotate = lambda *args: None
        kts.SetReflect = lambda *args: None

        # kts.DebugPrint(msg) - print the message to stdout, so debug
        # output from mods' initialisation code is visible when running
        # the editor from a terminal. (The game falls back to opening a
        # console window on Windows; we don't bother.)
        def debug_print(msg=None, *extra):
            if isinstance(msg, bytes):
                msg = msg.decode('utf-8', errors='replace')
            elif msg is None:
                msg = ''
            print(str(msg), flush=True)
        kts.DebugPrint = debug_print

        # kts.SetOverlayOffsets - retained so the anim preview can place
        # weapon overlays where the game would.
        def set_overlay_offsets(*args):
            parsed = parse_overlay_offsets(args)
            if parsed is not None:
                loader.overlay_offsets = parsed
        kts.SetOverlayOffsets = set_overlay_offsets

        # Math stubs
        kts.RandomRange = lambda a, b: int(a)
        kts.RandomChance = lambda p: False

        # Action/predicate constants
        for name in ['Activate', 'Can_Activate', 'Can_PickUp', 'Can_Drop',
                     'Can_SwingOrDrop', 'Can_Throw', 'Can_ThrowOrShoot',
                     'Throw', 'ThrowOrShoot', 'IsDoorOpen',
                     'CrystalStart', 'CrystalStop']:
            setattr(kts, name, KtsObject(name, (), lua))

        # Gameplay functions that may be called during init but are no-ops for us
        for name in ['AddTile', 'AddMonster', 'AddMissile', 'FlashMessage',
                     'PlaySound', 'PopUpWindow', 'Delay', 'GiveItem',
                     'GetNumHeld', 'GetPos', 'IsAlive', 'DestroyCreature',
                     'WinGame', 'TeleportTo', 'SetRespawnFunction',
                     'LockDoor', 'PlaceItem', 'SetBladeTrap', 'AddTask',
                     'LayoutDungeon', 'CloseDoor', 'OpenDoor', 'GameTime',
                     'GetTiles', 'RemoveTile', 'GetAllPlayers', 'SetHomeFor',
                     'ClearHints', 'AddHint', 'ResendHints',
                     'AddMonsters', 'Preset']:
            # (_k=name: bind per iteration, not the loop's final value)
            setattr(kts, name,
                    lambda *args, _k=name, **kwargs: KtsObject(_k, args, lua))

        # Create mod table
        mod = self.lua.table()
        g.mod = mod

        # mod.RegisterMod - as in the game: create a fresh namespace table
        # (with reads falling back to _G) and swap the calling chunk's _ENV
        # to it, so each mod's globals are private to that mod. The declared
        # name is registered immediately, so mods requiring each other
        # cyclically get the (partially loaded) namespace rather than an
        # infinite loop.
        #
        # The namespace's __newindex records the Lua call site of the
        # first assignment to each global (later re-assignments hit the
        # now-existing key directly and are not seen), so the editor can
        # jump to where any mod global - a plain string included - was
        # defined. See global_definition().
        def py_register(name, version, ns):
            # The checks, and their order, are the game's (Mod_RegisterMod
            # in lua_module.cpp): name must be a string (a number is
            # converted, as lua_tostring does), must not already be
            # registered, and version, if present, must be a string or
            # number in dotted-integer form.
            if isinstance(name, bytes):
                name = name.decode('utf-8', errors='replace')
            elif isinstance(name, (int, float)) and not isinstance(name, bool):
                name = _lua_number_to_string(name)
            elif not isinstance(name, str):
                raise RuntimeError("mod.RegisterMod: 'name' must be a string")
            if name in loader._registered_mods:
                raise RuntimeError(
                    f"mod.RegisterMod: mod '{name}' is already registered")
            if version is None:
                version = ""
            elif isinstance(version, (int, float)) and not isinstance(version, bool):
                version = _lua_number_to_string(version)
            elif not isinstance(version, str):
                raise RuntimeError(
                    "mod.RegisterMod: 'version' must be a string if present")
            if version and _parse_version(version) is None:
                raise RuntimeError(
                    f"mod.RegisterMod: invalid version '{version}'")
            loader._registered_mods[name] = ns
            loader._mod_versions[name] = version
            loader._declared_by.setdefault(
                loader._current_mod_name, []).append(name)
            # In the game this is a VFS path ("/<module dir>/"); the
            # editor has no VFS, so it is the mod's real directory
            # instead. Mods append a relative path to it (e.g.
            # prefix .. "gfx/foo.bmp"), and the file stubs
            # (kts.Graphic, dofile, ...) os.path.join the result with
            # the current mod dir, which an absolute path overrides.
            loader._mod_prefixes[name] = \
                os.path.join(loader._current_mod_dir, "")
            loader._current_env = ns

        make_register_mod, self._lookup_global_def, lua_use_strict = \
            self.lua.eval('''
            (function()
              local getinfo = debug.getinfo
              local getupvalue = debug.getupvalue
              local setupvalue = debug.setupvalue
              local defs = {}   -- ns -> { global name -> {source, line} }
              local meta = {
                __index = _G,
                __newindex = function(t, k, v)
                  if defs[t][k] == nil then
                    local info = getinfo(2, "Sl")
                    if info then
                      defs[t][k] = { info.source, info.currentline }
                    end
                  end
                  rawset(t, k, v)
                end,
              }
              local make = function(py_register)
                return function(config)
                  local ns = setmetatable({}, meta)
                  defs[ns] = {}
                  py_register(config.name, config.version, ns)
                  if not config.keep_env then
                    local f = getinfo(2, "f").func
                    local upname = getupvalue(f, 1)
                    if upname == "_ENV" then
                      setupvalue(f, 1, ns)
                    end
                  end
                  return ns
                end
              end
              local lookup = function(ns, key)
                local d = defs[ns]
                d = d and d[key]
                if d then return d[1], d[2] end
              end
              -- use_strict, as in the game (lua_module.cpp UseStrict /
              -- StrictIndex / StrictNewIndex): swap the calling chunk's
              -- _ENV for an empty proxy whose metamethods forward to the
              -- old environment. Reading a never-assigned global errors;
              -- assigning an undeclared global errors unless done from a
              -- main chunk's top level (init.lua or a dofile'd file),
              -- which declares it. "x = nil" at top level therefore
              -- declares x, exactly as documented.
              --
              -- Lives in this chunk so the proxy's __newindex can record
              -- definition sites in defs itself: with the proxy in
              -- between, the ns __newindex above would see the proxy
              -- function, not the mod code, as the assignment site.
              local use_strict = function()
                local f = getinfo(2, "f").func
                local upname, oldenv = getupvalue(f, 1)
                if upname ~= "_ENV" then
                  error("use_strict: bad upvalue")
                end
                local declared = {}
                local proxy = setmetatable({}, {
                  __index = function(t, k)
                    local v = oldenv[k]
                    if v ~= nil then
                      return v
                    elseif rawget(declared, k) then
                      return nil
                    else
                      -- level 2 = the reading code (the game's message
                      -- has no position prefix; adding one helps here)
                      error("Variable '" .. tostring(k)
                            .. "' is not declared", 2)
                    end
                  end,
                  __newindex = function(t, k, v)
                    if not rawget(declared, k) then
                      local info = getinfo(2, "S")
                      if not (info and info.what == "main") then
                        error("Assignment to undeclared variable '"
                              .. tostring(k) .. "'", 2)
                      end
                      declared[k] = true
                    end
                    local d = defs[oldenv]
                    if d and d[k] == nil then
                      local info = getinfo(2, "Sl")
                      if info then
                        d[k] = { info.source, info.currentline }
                      end
                    end
                    oldenv[k] = v
                  end,
                })
                setupvalue(f, 1, proxy)
              end
              return make, lookup, use_strict
            end)()
        ''')
        mod.RegisterMod = make_register_mod(py_register)

        # The "optional dependency" APIs - IsModRegistered, GetModVersion,
        # GetModPrefix and GetRegisteredMod - all start by asking whether
        # a named mod is registered. As in the game, an unloaded mod is
        # simply not registered. In on-demand mode the named mod is
        # first loaded from disk if it is installed, so every installed
        # optional dependency ends up in the inferred load order
        # whichever of these APIs a mod uses to test for it (e.g.
        # `if mod.IsModRegistered("x") then require("x") ...`).
        #
        # An optional dependency whose own loading fails is treated as
        # absent from then on and noted in failed_optional (the dialog
        # reports it): it is optional, so the mod being edited should
        # still load, just as it would in the game for a user who
        # doesn't enable the broken mod. A mod part-way through loading
        # (a cycle) is likewise just "not registered yet".
        def optional_lookup(api, name):
            if not isinstance(name, str):
                raise RuntimeError(f"mod.{api}: arg 1 must be a string")
            loader._record_dependency(name, optional=True)
            if (loader.on_demand
                    and name not in loader._registered_mods
                    and name not in loader.failed_optional
                    and name not in loader._loading
                    and loader.find_module_dir(name) is not None):
                try:
                    loader.load_mod(name)
                except Exception as e:
                    loader.failed_optional[name] = e
                    # The failing mod recorded the loading stack; that
                    # is not the error of the load in progress.
                    loader.error_stack = None

        def is_mod_registered(name):
            optional_lookup("IsModRegistered", name)
            return name in loader._registered_mods
        mod.IsModRegistered = is_mod_registered

        # GetRegisteredMod: the mod's namespace, or nil if it isn't
        # registered (or is older than min_version).
        def get_registered_mod(name, min_version=None):
            if not isinstance(name, str):
                raise RuntimeError("mod.GetRegisteredMod: arg 1 must be a string")
            if min_version is not None:
                if not isinstance(min_version, str):
                    raise RuntimeError(
                        "mod.GetRegisteredMod: min_version must be a string")
                if _parse_version(min_version) is None:
                    raise RuntimeError(
                        f"mod.GetRegisteredMod: invalid min_version "
                        f"'{min_version}'")
            optional_lookup("GetRegisteredMod", name)
            ns = loader._registered_mods.get(name)
            if ns is None:
                return None
            if min_version is not None and not version_ge(
                    loader._mod_versions.get(name, ""), min_version):
                return None
            return ns
        mod.GetRegisteredMod = get_registered_mod

        # As in the game: the version string passed to mod.RegisterMod
        # ("" if none was given), or nil for unregistered mods.
        def get_mod_version(name):
            optional_lookup("GetModVersion", name)
            return loader._mod_versions.get(name)
        mod.GetModVersion = get_mod_version

        # Path prefix for reading files from another (registered) mod;
        # a real directory here rather than the game's VFS path (see
        # py_register above). nil for unregistered mods.
        def get_mod_prefix(name):
            optional_lookup("GetModPrefix", name)
            return loader._mod_prefixes.get(name)
        mod.GetModPrefix = get_mod_prefix
        # As in the game: a table of declared name -> version string.
        mod.GetAllMods = lambda: lua.table_from(dict(loader._mod_versions))

        # use_strict - strict global checking, defined alongside
        # mod.RegisterMod above (they share the defs table).
        g.use_strict = lua_use_strict

        # dofile - the file is looked up as for any file a mod names (see
        # resolve_path), and while its chunk runs the "current directory"
        # is the file's own, so files it names relative to itself are
        # found, as in the game. As in the game, the chunk runs in the
        # *caller's* environment (the "dofile namespace proposal"), so
        # files dofile'd after mod.RegisterMod see and populate that
        # mod's namespace.
        def py_begin_dofile(filename):
            filepath = loader.resolve_path(filename)
            if not os.path.isfile(filepath):
                raise FileNotFoundError(
                    f"dofile: {filename}: file not found "
                    f"(looked in {loader._current_dir})")
            code = read_bytes_as_text(filepath)
            loader.loaded_files.append(filepath)
            loader._dir_stack.append(loader._current_dir)
            loader._current_dir = os.path.dirname(filepath)
            return code, "@" + filepath

        def py_end_dofile():
            loader._current_dir = loader._dir_stack.pop()

        make_dofile = self.lua.eval('''
            (function()
              local getinfo = debug.getinfo
              local getupvalue = debug.getupvalue
              local load = load
              local pack, unpack = table.pack, table.unpack
              return function(py_begin, py_end)
                return function(filename)
                  local code, chunkname = py_begin(filename)
                  local env = _G
                  local f = getinfo(2, "f").func
                  local upname, upval = getupvalue(f, 1)
                  if upname == "_ENV" then
                    env = upval
                  end
                  local chunk, err = load(code, chunkname, "t", env)
                  if not chunk then
                    py_end()
                    error(err)
                  end
                  local results = pack(chunk())
                  py_end()
                  return unpack(results, 1, results.n)
                end
              end
            end)()
        ''')
        g.dofile = make_dofile(py_begin_dofile, py_end_dofile)

        # require - return a registered mod's namespace. As in the game,
        # it is an error if the mod hasn't been loaded (i.e. isn't earlier
        # in the load order). In on-demand mode the module is loaded from
        # disk instead; that is how discover_load_order() infers an order:
        # dependencies finish loading before their dependents.
        def lua_require(name):
            if not isinstance(name, str):
                raise RuntimeError("require: arg 1 must be a string")
            loader._record_dependency(name, optional=False)
            ns = loader._registered_mods.get(name)
            if ns is not None:
                return ns
            if not loader.on_demand:
                raise RuntimeError(
                    f"require: mod '{name}' is not registered (it must be "
                    f"enabled, and come earlier, in the module load order)")
            if loader.find_module_dir(name) is None:
                raise RuntimeError(
                    f"Module '{name}' not found "
                    f"({loader._describe_missing_module(name)})")
            ns = loader.load_mod(name)
            if ns is None:
                # Loaded, but its init.lua never called mod.RegisterMod
                raise RuntimeError(f"require: mod '{name}' is not registered")
            return ns
        g['require'] = lua_require

        # Run a chunk with an explicit chunkname ("@" + path), so
        # debug.getinfo reports real source locations for code in
        # init.lua (dofile'd files already get this via py_read_mod_file).
        #
        # As in the game (LuaExecRStreamReadOnly), the chunk's _ENV is a
        # read-only proxy: reads fall through to _G, but assigning a
        # global is an error. So a mod's globals can only live in the
        # namespace mod.RegisterMod gives it (which replaces the proxy as
        # _ENV), and a global assigned before RegisterMod - or by a mod
        # that never calls it - fails here as it would in the game,
        # rather than landing in _G where every other mod would see it.
        # The game's message has no position prefix; level 2 adds the
        # mod's own file and line.
        self._run_chunk = self.lua.eval('''
            (function()
              local load, assert, setmetatable, error, tostring =
                load, assert, setmetatable, error, tostring
              local meta = {
                __index = _G,
                __newindex = function(t, k, v)
                  error("attempt to write to global '" .. tostring(k)
                        .. "' (read-only environment)", 2)
                end,
                __metatable = false,
              }
              return function(code, chunkname)
                local env = setmetatable({}, meta)
                return assert(load(code, chunkname, "t", env))()
              end
            end)()
        ''')

        # Retained for LuaInspector (debug is stripped from _G below).
        self.fn_source_info = self.lua.eval('''
            (function()
              local getinfo = debug.getinfo
              return function(f)
                local i = getinfo(f, "S")
                return i.source, i.linedefined
              end
            end)()
        ''')

        self._apply_sandbox_whitelist()

    def _apply_sandbox_whitelist(self):
        """Strip the Lua environment down to what the game's sandbox allows.

        Mirrors the whitelists in the game's src/shared/impl/lua_sandbox.cpp
        (MakeLuaSandbox), so that a mod using anything outside the game's
        sandbox (os, io, load, math.pow, table.maxn, ...) fails in the
        editor just as it would in the game. The game's lists are tuned
        for Lua 5.4 without LUA_COMPAT_5_3, so the 5.3-compat names
        (math.atan2, math.pow, ...) are deliberately absent.

        This also removes lupa's "python" global, which would otherwise
        give mods full access to the Python interpreter.

        Differences from the game, all deliberate:
          - "print" is kept (the game strips stock print but installs its
            own; the editor's stand-in is stock print to stdout);
          - the editor's own globals (kts, mod, dofile, require,
            use_strict) are kept - the game re-adds its versions of these
            after whitelisting, in AddModuleFuncs / AddLuaConfigFunctions.
        """
        self.lua.execute('''
            local function apply(tbl, keep)
              local set = {}
              for _, k in ipairs(keep) do set[k] = true end
              local kill = {}
              for k in pairs(tbl) do
                if not set[k] then kill[#kill + 1] = k end
              end
              for _, k in ipairs(kill) do tbl[k] = nil end
            end

            apply(_G, {
              "_G", "_VERSION", "assert", "coroutine", "error",
              "getmetatable", "ipairs", "math", "next", "pairs", "pcall",
              "rawequal", "rawget", "rawlen", "rawset", "select",
              "setmetatable", "string", "table", "tonumber", "tostring",
              "type", "utf8", "xpcall",
              -- editor additions (see docstring):
              "kts", "mod", "dofile", "require", "use_strict", "print",
            })
            apply(coroutine, {
              "close", "create", "isyieldable", "resume", "running",
              "status", "wrap", "yield",
            })
            apply(string, {
              "byte", "char", "find", "format", "gmatch", "gsub", "len",
              "lower", "match", "pack", "packsize", "rep", "reverse",
              "sub", "unpack", "upper",
            })
            apply(table, {
              "concat", "insert", "move", "pack", "remove", "sort",
              "unpack",
            })
            apply(math, {
              "abs", "acos", "asin", "atan", "ceil", "cos", "deg", "exp",
              "floor", "fmod", "huge", "log", "max", "maxinteger", "min",
              "mininteger", "modf", "pi", "rad", "random", "randomseed",
              "sin", "sqrt", "tan", "tointeger", "type", "ult",
            })
            apply(utf8, {
              "char", "charpattern", "codepoint", "codes", "len",
              "offset",
            })
        ''')

    def _record_dependency(self, name: str, optional: bool):
        """Note that the mod currently loading referenced `name` via
        require() (optional=False) or mod.GetRegisteredMod()."""
        if self._current_mod_name is None:
            return
        edges = self.optional_mods if optional else self.required_mods
        lst = edges.setdefault(self._current_mod_name, [])
        if name not in lst:
            lst.append(name)

    def load_sequence(self, order: list[str], mod_dir: str | None = None):
        """Load mods one after another in the given order, as the game
        does with modules.txt.

        `mod_dir` is the directory of the mod being edited; the entry in
        `order` with the same (basename) name is loaded from there rather
        than from the modules search path, so a mod living outside every
        modules directory (or shadowing an installed one) can be edited.
        Every other name is looked up in the modules search path.

        Stops at the first error, leaving the mods loaded so far in
        place (so the editor can still show them).
        """
        edited_name = None
        if mod_dir is not None:
            edited_name = os.path.basename(os.path.normpath(mod_dir))
        for name in order:
            if name == edited_name:
                self.load_mod_dir(mod_dir, name)
                continue
            if self.find_module_dir(name) is None:
                raise RuntimeError(
                    f"Module '{name}' is in the load order but is not "
                    f"installed ({self._describe_missing_module(name)})")
            self.load_mod(name)

    def find_module_dir(self, mod_name: str) -> str | None:
        """Directory of the installed mod `mod_name` (first match in the
        modules search path), or None if it isn't installed."""
        return find_module_dir(mod_name, self.modules_dirs)

    def resolve_path(self, filename: str) -> str:
        """The file a name in mod Lua (dofile, kts.Graphic, kts.Sound,
        kts.LoadSegments) refers to, as a filesystem path.

        A relative name is taken relative to the directory of the Lua
        file being executed (the mod directory for init.lua, a dofile'd
        file's own directory while it runs), as the game does. An
        absolute name is used as-is; that is how a mod refers to another
        mod's files, via mod.GetModPrefix(), which in the editor is a
        real directory. (The game additionally falls back to a virtual
        root in which each mod is a top-level directory; that
        undocumented form is not supported here.)
        """
        if os.path.isabs(filename):
            return filename
        return os.path.join(self._current_dir, filename)

    def _describe_missing_module(self, mod_name: str) -> str:
        """Where a missing mod was looked for, for error messages."""
        if len(self.modules_dirs) == 1:
            return f"no init.lua in {self.modules_dirs[0]}/{mod_name}"
        return (f"no {mod_name}/init.lua in any modules directory: "
                + ", ".join(self.modules_dirs))

    def load_mod(self, mod_name: str):
        """Load a mod by name from the modules search path. The mod must
        be installed (see find_module_dir)."""
        mod_dir = self.find_module_dir(mod_name)
        if mod_dir is None:
            raise RuntimeError(f"Module '{mod_name}' not found "
                               f"({self._describe_missing_module(mod_name)})")
        return self.load_mod_dir(mod_dir, mod_name)

    def load_mod_dir(self, mod_dir: str, mod_name: str | None = None):
        """Load a mod from an explicit directory.

        In on-demand mode, any modules it requires are loaded on demand
        from the modules directory (see the require/GetRegisteredMod
        stubs), so dependencies need not be loaded beforehand; otherwise
        they must already have been loaded (see load_sequence). Returns
        the mod's namespace.
        """
        if mod_name is None:
            mod_name = os.path.basename(os.path.normpath(mod_dir))
        if mod_name in self._mod_dirs:
            # Already loaded (None if it never called mod.RegisterMod)
            return self._mod_namespaces.get(mod_name)
        if mod_name in self._loading:
            raise RuntimeError(
                f"Circular dependency detected while loading '{mod_name}'")

        init_file = os.path.join(mod_dir, "init.lua")
        old_mod_dir = self._current_mod_dir
        old_mod_name = self._current_mod_name
        old_env = self._current_env
        self._current_mod_dir = mod_dir
        old_dir, old_stack_depth = self._current_dir, len(self._dir_stack)
        self._current_dir = mod_dir
        self._current_mod_name = mod_name
        self._current_env = None
        self._loading.add(mod_name)
        self._loading_stack.append(mod_name)

        try:
            print(f"Loading mod: {mod_name}")
            code = read_bytes_as_text(init_file)
            self.loaded_files.append(init_file)
            # The chunk starts out in a read-only view of _G (see
            # _run_chunk); mod.RegisterMod swaps its _ENV to a fresh
            # namespace table and records it in _current_env.
            self._run_chunk(code, "@" + init_file)

            ns = self._current_env
            if ns is not None:
                self._mod_namespaces[mod_name] = ns
            # else: never called mod.RegisterMod. As in the game it is
            # loaded but not registered: require() of it fails, and it
            # has no namespace (it cannot have assigned any globals).
            self._mod_dirs[mod_name] = mod_dir
            self.load_order.append(mod_name)
            return ns
        except Exception:
            # Innermost frame records the stack; outer frames see it set.
            if self.error_stack is None:
                self.error_stack = list(self._loading_stack)
            # Whatever this mod registered before failing is withdrawn:
            # the game never gets as far as anyone seeing it, and here
            # a later lookup (e.g. of a failed optional dependency) must
            # not find a half-loaded namespace.
            for declared in self._declared_by.pop(mod_name, []):
                self._registered_mods.pop(declared, None)
                self._mod_versions.pop(declared, None)
                self._mod_prefixes.pop(declared, None)
            raise
        finally:
            self._loading.discard(mod_name)
            self._loading_stack.pop()
            self._current_mod_dir = old_mod_dir
            # (A dofile whose chunk raised never popped its entry.)
            del self._dir_stack[old_stack_depth:]
            self._current_dir = old_dir
            self._current_mod_name = old_mod_name
            self._current_env = old_env

    def get_tile_table(self) -> dict[int, TileEntry]:
        """Fallback tile table for files not loaded via kts.LoadSegments.

        Searches loaded mods (most recently loaded first) for a global
        named tile_table, and returns it as a dict mapping tile number
        (1-based) to TileEntry.
        """
        source = self.fallback_tile_table_source()
        if source is None:
            return {}
        return self._convert_lua_tile_table(source[2])

    def fallback_tile_table_source(self) -> tuple[str, str, object] | None:
        """Where get_tile_table's fallback table comes from: a tuple
        (mod name, global name, raw Lua table) naming the most recently
        loaded mod that defines a global called tile_table, or None if
        no loaded mod does."""
        rawget = self.lua.eval("rawget")
        for name in reversed(self.load_order):
            ns = self._mod_namespaces.get(name)
            if ns is None:
                continue
            lua_tile_table = rawget(ns, "tile_table")
            if lua_tile_table is not None:
                return name, "tile_table", lua_tile_table
        return None

    def get_mod_dirs(self) -> dict[str, str]:
        """Directory each loaded mod was loaded from, keyed by mod name."""
        return dict(self._mod_dirs)

    def get_file_tile_table(self, filepath: str) -> dict[int, TileEntry] | None:
        """Get the tile table for a specific segment file.

        Returns the (converted) tile table that was passed to
        kts.LoadSegments for this file, or None if the file was never
        loaded via kts.LoadSegments. Conversion happens on demand so
        that tile mutations made after the LoadSegments call are
        reflected.
        """
        lua_tile_table = self._file_tile_tables.get(os.path.realpath(filepath))
        if lua_tile_table is None:
            return None
        return self._convert_lua_tile_table(lua_tile_table)

    def get_namespaces(self) -> list[tuple[str, object]]:
        """(mod name, namespace table) for each loaded mod, in load order.

        The namespace is the table mod.RegisterMod created for the mod;
        mods that never called it have none and are omitted.
        """
        return [(n, self._mod_namespaces[n]) for n in self.load_order
                if n in self._mod_namespaces]

    def global_definition(self, ns, name: str) -> tuple[str, int] | None:
        """(file path, line) of the assignment that first created the
        global `name` in the namespace table `ns`, if recorded. Only
        mod.RegisterMod namespaces record assignments (raw _G does not),
        and only the first assignment to each name is seen."""
        try:
            result = self._lookup_global_def(ns, name)
        except Exception:
            return None
        if not isinstance(result, tuple):
            return None
        source, line = result
        if isinstance(source, bytes):
            source = source.decode('utf-8', 'replace')
        if not isinstance(source, str) or not source.startswith("@"):
            return None
        try:
            return source[1:], int(line)
        except (TypeError, ValueError):
            return None

    def scan_object_names(self) -> dict[int, tuple[str, str]]:
        """Map id(stub object) -> (mod name, global name) it is bound to.

        Scans each loaded mod's namespace (in load order, first name
        found wins) for globals holding kts stub objects, so they can be
        displayed by their Lua names (e.g. "t_floor1"). Lua next() only
        sees a table's own keys, so the __index = _G fallback on mod
        namespaces doesn't cause duplicates.
        """
        names: dict[int, tuple[str, str]] = {}
        for mod_name, ns in self.get_namespaces():
            try:
                entries = list(ns.items())
            except Exception:
                continue
            for k, v in entries:
                if isinstance(k, str) and isinstance(v, KtsObject):
                    names.setdefault(id(v), (mod_name, k))
        return names

    def get_mod_load_segments_calls(self,
                                    mod_dir: str) -> list[LoadSegmentsCall]:
        """The kts.LoadSegments calls made from Lua files inside mod_dir
        (dependency mods' calls are excluded)."""
        root = os.path.realpath(mod_dir)
        return [c for c in self._load_segments_calls
                if c.source
                and os.path.realpath(c.source).startswith(root + os.sep)]

    def suggest_tile_table(self, mod_dir: str):
        """Suggest a tile table for a new segment file in mod_dir.

        Returns (expr, lua_table): expr is Lua source text referencing
        the table (default "tile_table"), and lua_table is the raw Lua
        table from the mod's last existing LoadSegments call, or None
        if the mod makes no such calls.
        """
        calls = self.get_mod_load_segments_calls(mod_dir)
        if not calls:
            return "tile_table", None
        call = calls[-1]
        return call.tile_table_expr() or "tile_table", call.tile_table

    def register_segment_file(self, filepath: str, lua_tile_table):
        """Manually associate a tile table with a segment file (used for
        newly created files not yet referenced by any Lua code)."""
        self._file_tile_tables[os.path.realpath(filepath)] = lua_tile_table

    def _convert_lua_tile_table(self, lua_tile_table) -> dict[int, TileEntry]:
        # id(stub object) -> its Lua global name, for display
        names = {i: name for i, (_, name) in self.scan_object_names().items()}
        result = {}
        for i, entry in lua_tile_table.items():
            # The game only ever looks tiles up by integer tile number
            # (segment.cpp), so any other keys a mod keeps on the table
            # (helper functions, string-keyed metadata, ...) are simply
            # not tiles. Lua has already folded integral floats (1.0)
            # into integers; bool is excluded explicitly as Python
            # treats it as an int.
            if isinstance(i, bool) or not isinstance(i, int):
                continue
            tile_entry = self._convert_tile_entry(entry, names)
            if tile_entry is not None:
                result[i] = tile_entry

        return result

    def _convert_tile_entry(self, entry,
                            names: dict[int, str]) -> TileEntry | None:
        """Convert a Lua tile_table entry to a TileEntry.

        Mirrors what the game accepts (segment.cpp readTile): a Tile, a
        bare ItemType or MonsterType, or a table of those where items may
        also appear as {itemtype, count} pairs. Anything else (a Graphic,
        a function, ...) is not something the game could place either,
        and is ignored. Note that hasattr() can't be used to detect Lua
        tables here: KtsObject.__getattr__ answers None for any unknown
        name, so hasattr(stub, 'items') is always true.
        """
        if entry is None:
            return None

        if isinstance(entry, KtsTile):
            return TileEntry(tiles=[entry.to_tile_info(names.get(id(entry)))])

        if isinstance(entry, (KtsItem, KtsMonster)):
            info = self._item_tile_info(entry, 1, names)
            return TileEntry(tiles=[info]) if info else None

        # A Lua table: composite like {t_door, t_background} or
        # {t_floor1, {i_dagger, 8}}.
        if _is_lua_table(entry):
            tiles = []
            for _, v in entry.items():
                if isinstance(v, KtsTile):
                    tiles.append(v.to_tile_info(names.get(id(v))))
                elif isinstance(v, (KtsItem, KtsMonster)):
                    info = self._item_tile_info(v, 1, names)
                    if info:
                        tiles.append(info)
                elif _is_lua_table(v):
                    # Nested {item, count} table, e.g. {B.i_dagger, 8}
                    item = None
                    count = 1
                    for _, vv in v.items():
                        if isinstance(vv, (KtsItem, KtsMonster)):
                            item = vv
                        elif isinstance(vv, (int, float)) and not isinstance(vv, bool):
                            count = int(vv)
                    if item is not None:
                        info = self._item_tile_info(item, count, names)
                        if info:
                            tiles.append(info)
            return TileEntry(tiles=tiles) if tiles else None

        return None

    @staticmethod
    def _item_tile_info(obj, count: int, names: dict[int, str]) -> TileInfo | None:
        """TileInfo showing an item (with its stack-count graphic) or a
        monster, drawn in front of the tiles under it; None if it has no
        graphic to show."""
        if isinstance(obj, KtsItem):
            gfx = obj.get_display_graphic(count)
        else:
            gfx = obj.get_graphic()
        if not gfx:
            return None
        return TileInfo(graphic=gfx, depth=-1, name=names.get(id(obj)))
