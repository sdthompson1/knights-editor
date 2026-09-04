"""Data model for the Knights Mod Editor."""

import copy
import re
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class Graphic:
    """A single graphic image reference, as defined by kts.Graphic()."""
    filename: str
    color_key: Optional[tuple[int, int, int]] = None
    handle_pos: tuple[int, int] = (0, 0)


@dataclass
class TileInfo:
    """Info about a single tile object, as defined by kts.Tile()."""
    graphic: Optional[Graphic] = None
    open_graphic: Optional[Graphic] = None
    depth: int = 0
    # The Lua global name the object is bound to (e.g. "t_floor1",
    # "i_necronomicon"), if one could be found.
    name: Optional[str] = None
    # Walking access from the tile's `access` table: "blocked" (walls),
    # "approach" (doors, chests, switches...), "clear" (floor), or None
    # if not a tile / not specified.
    walking: Optional[str] = None
    # Whether the tile can be smashed (non-zero `hit_points`), after which
    # it is replaced by floor.
    destructible: bool = False
    # Whether the tile is a door or gate (`type = "door"`), which a knight
    # can open (with a key, lock picks or a switch) and then stand in.
    is_door: bool = False
    # The tile's `connectivity_check` override for the engine's
    # key-reachability check (1 = always passable, -1 = always impassable,
    # 0 = default). See tile_standable for how the room checks use it.
    connectivity_check: int = 0


# Ranking of walking access values, most restrictive first.
_WALKING_RANK = {"blocked": 2, "approach": 1, "clear": 0}


def tile_standable(t: "TileInfo") -> bool:
    """Whether a knight could ever stand on this tile: it is walkable now,
    or can be smashed or opened to become walkable.

    This follows the engine's connectivity check (dungeon_generator.cpp)
    except for connectivity_check = -1. That flag makes the key-
    reachability check conservatively treat a tile as a wall, and is only
    ever applied to tiles a knight might otherwise end up on: closed gates
    (plain tiles that a switch swaps for an open gate), open pits (clear
    access, but deadly), doors guarding a switch. For room layout we must
    be conservative the other way and assume a knight may stand there, so
    any non-zero connectivity_check counts as standable."""
    if t.connectivity_check != 0:
        return True
    if t.walking is None or t.walking == "clear":
        return True
    return t.destructible or t.is_door


def entry_standable(entry: Optional["TileEntry"]) -> bool:
    """Whether a knight could ever stand on a tile_table square: every
    tile on it must be standable (see tile_standable). Items and monsters
    in the entry have no access info and never block. Squares whose entry
    is missing count as standable, the safe assumption for the room
    checks."""
    if not entry:
        return True
    return all(tile_standable(t) for t in entry.tiles)


def entry_walking(entry: Optional["TileEntry"]) -> str:
    """Effective walking access of a tile_table entry: the most restrictive
    of its component tiles ("blocked" / "approach" / "clear"). Entries with
    no access info count as "clear"."""
    worst = "clear"
    if entry:
        for t in entry.tiles:
            if t.walking in _WALKING_RANK and _WALKING_RANK[t.walking] > _WALKING_RANK[worst]:
                worst = t.walking
    return worst


@dataclass
class TileEntry:
    """An entry in the tile_table. May be a single tile or a composite
    (e.g. {t_door_horiz, t_hdoor_background}). Non-tile elements like
    items or monsters are ignored for display purposes."""
    tiles: list[TileInfo] = field(default_factory=list)


# Switch functions whose first two args are the x/y offset of a target tile.
# Any other ("custom") function's args have no editor-known interpretation.
TARGET_FUNCS = {"toggle", "open", "close", "shoot"}

# shoot's third arg, 1-based (the game's rf_shoot does
# select(dir_as_num, "north", "east", "south", "west")).
SHOOT_DIRECTIONS = ("north", "east", "south", "west")


@dataclass
class SwitchFunc:
    """A single function within a switch definition."""
    name: str        # e.g. "toggle", "open", "close", "shoot"
    args: list[int]  # e.g. [dx, dy] for toggle/open/close, [dx, dy, dir] for shoot

    @property
    def has_target(self) -> bool:
        """True if args[0:2] are the x/y offset of a target tile."""
        return self.name in TARGET_FUNCS

    @property
    def dx(self) -> int:
        return self.args[0] if self.args else 0

    @property
    def dy(self) -> int:
        return self.args[1] if len(self.args) > 1 else 0

    @property
    def direction(self) -> str | None:
        """For shoot: the direction string, or None if the direction
        value is missing or not 1-4 (the game would fail on such a value
        when the switch is used; the editor just shows it as unknown)."""
        if self.name == "shoot" and len(self.args) >= 3 \
                and 1 <= self.args[2] <= len(SHOOT_DIRECTIONS):
            return SHOOT_DIRECTIONS[self.args[2] - 1]
        return None

    def to_text(self) -> str:
        """Editable "name(arg1,arg2,...)" notation (see parse_text)."""
        return f"{self.name}({','.join(str(a) for a in self.args)})"

    @staticmethod
    def parse_text(text: str) -> 'SwitchFunc':
        """Parse "name(arg1,arg2,...)" notation; raises ValueError."""
        m = re.fullmatch(r"\s*([A-Za-z_]\w*)\s*\((.*)\)\s*", text)
        if not m:
            raise ValueError(
                f'Expected "name(arg, arg, ...)", got: {text.strip()!r}')
        name, arg_str = m.group(1), m.group(2).strip()
        args = []
        if arg_str:
            for part in arg_str.split(","):
                try:
                    args.append(int(part.strip()))
                except ValueError:
                    raise ValueError(
                        f"Arguments must be integers, got: {part.strip()!r}")
        return SwitchFunc(name, args)

    def description(self) -> str:
        if self.name == "shoot":
            dir_str = self.direction or "?"
            return f"shoot ({self.dx:+d}, {self.dy:+d}) {dir_str}"
        if not self.has_target:
            return f"{self.name}({', '.join(str(a) for a in self.args)})"
        return f"{self.name} ({self.dx:+d}, {self.dy:+d})"


@dataclass
class Switch:
    """A switch definition within a segment."""
    x: int
    y: int
    raw_text: str        # the full raw line (preserved for round-tripping)
    funcs: list[SwitchFunc] = field(default_factory=list)

    def rebuild_raw_text(self):
        """Regenerate raw_text from x, y and funcs after editing them."""
        parts = [str(self.x), str(self.y), str(len(self.funcs))]
        for func in self.funcs:
            parts.append(func.name)
            parts.append(str(len(func.args)))
            parts.extend(str(a) for a in func.args)
        self.raw_text = " ".join(parts)

    @staticmethod
    def parse(raw_text: str) -> 'Switch':
        """Parse a raw switch line ("x y nfuncs [name nargs args...]...")
        into a Switch with structured funcs. Raises ValueError if the
        line is malformed or has anything left over after the last
        function."""
        tokens = raw_text.split()
        pos = 0

        def take(what: str) -> str:
            nonlocal pos
            if pos >= len(tokens):
                raise ValueError(f"missing {what} in switch line {raw_text.strip()!r}")
            tok = tokens[pos]
            pos += 1
            return tok

        def take_int(what: str, minimum: int | None = None) -> int:
            tok = take(what)
            try:
                value = int(tok)
            except ValueError:
                raise ValueError(f"expected {what} (an integer), got {tok!r} "
                                 f"in switch line {raw_text.strip()!r}")
            if minimum is not None and value < minimum:
                raise ValueError(f"{what} must be at least {minimum}, got {value} "
                                 f"in switch line {raw_text.strip()!r}")
            return value

        x = take_int("switch x")
        y = take_int("switch y")
        num_funcs = take_int("number of functions", 0)
        funcs = []
        for _ in range(num_funcs):
            name = take("function name")
            nargs = take_int(f"number of arguments to {name}", 0)
            args = [take_int(f"argument {j + 1} to {name}") for j in range(nargs)]
            funcs.append(SwitchFunc(name, args))
        if pos != len(tokens):
            raise ValueError(f"unexpected extra values {' '.join(tokens[pos:])!r} "
                             f"at the end of switch line {raw_text.strip()!r}")
        return Switch(x, y, raw_text, funcs)


@dataclass
class Room:
    """A room rectangle within a segment."""
    x: int
    y: int
    w: int
    h: int


# Smallest segment the editor will create by resizing. The outer wall
# ring the dungeon generator adds is not counted, so even a 1x1 segment
# is a usable 3x3 room.
MIN_SEGMENT_SIZE = 1


def _switch_fits(sw: Switch, width: int, height: int) -> bool:
    """Whether the switch's tile exists in a width x height segment."""
    return 0 <= sw.x < width and 0 <= sw.y < height


def _room_fits(room: Room, width: int, height: int) -> bool:
    """Whether the whole room lies within a width x height segment plus
    its outer wall ring (room coords 0..width+1 / 0..height+1)."""
    return (room.x >= 0 and room.x + room.w - 1 <= width + 1
            and room.y >= 0 and room.y + room.h - 1 <= height + 1)


@dataclass
class MapBlock:
    """A rectangular area cut or copied from a segment (see
    Segment.extract), ready to be pasted into a segment (Segment.paste).

    Positions are relative to the block's top-left tile, in the same
    conventions as a Segment: switches in data tile coords, rooms in
    room coords (offset by one). A block may extend onto a segment's
    outer wall ring, which has no tile data: such tiles are None and
    leave the destination tile untouched when pasted.
    """
    width: int
    height: int
    tiles: list[list[int | None]]  # tiles[row][col]
    switches: list[Switch] = field(default_factory=list)
    rooms: list[Room] = field(default_factory=list)


@dataclass
class Segment:
    """A map segment (room) containing a grid of tile numbers."""
    name: str
    width: int
    height: int
    data: list[list[int]]  # data[row][col], each value is a tile number (1-based)
    rooms: list[Room] = field(default_factory=list)
    switches: list[Switch] = field(default_factory=list)
    # Where the segment was loaded from (set by segment_io.load_segments):
    # the file path and the segment's position in that file. Excluded from
    # equality so segments still compare by content.
    source_file: str | None = field(default=None, compare=False, repr=False)
    source_index: int | None = field(default=None, compare=False, repr=False)

    def resized(self, width: int, height: int, fill: int) -> "Segment":
        """A copy of this segment resized to width x height, keeping the
        top-left corner fixed: tiles beyond the new size are dropped and
        newly exposed tiles are set to `fill`.

        Switches whose tile is dropped are discarded with it, as is any
        room that no longer fits within the new segment and its outer
        wall ring."""
        data = [[self.data[r][c] if r < len(self.data) and c < len(self.data[r])
                 else fill
                 for c in range(width)] for r in range(height)]
        rooms = [copy.deepcopy(r) for r in self.rooms if _room_fits(r, width, height)]
        switches = [copy.deepcopy(sw) for sw in self.switches
                    if _switch_fits(sw, width, height)]
        return Segment(self.name, width, height, data, rooms, switches,
                       source_file=self.source_file,
                       source_index=self.source_index)

    # --- Rectangular cut/copy/paste (see MapBlock) ---
    #
    # Rectangles are given in data tile coords (col, row, w, h) and may
    # extend onto the outer wall ring (col/row of -1, or width/height).

    def _in_data(self, col: int, row: int) -> bool:
        return 0 <= col < self.width and 0 <= row < self.height

    def _rooms_within(self, col: int, row: int, w: int, h: int) -> list[Room]:
        """The rooms lying entirely inside the rectangle (walls included),
        which is what a cut or copy of it takes with it."""
        # Room coords are data coords + 1, so the rectangle spans room
        # coords col+1 .. col+w (and likewise for rows).
        return [r for r in self.rooms
                if r.x >= col + 1 and r.x + r.w - 1 <= col + w
                and r.y >= row + 1 and r.y + r.h - 1 <= row + h]

    def extract(self, col: int, row: int, w: int, h: int) -> MapBlock:
        """A copy of the rectangle's tiles, together with the switches on
        them and the rooms lying entirely inside it. The segment is not
        changed."""
        tiles = [[self.data[r][c] if self._in_data(c, r) else None
                  for c in range(col, col + w)]
                 for r in range(row, row + h)]
        switches = []
        for sw in self.switches:
            if col <= sw.x < col + w and row <= sw.y < row + h:
                sw = copy.deepcopy(sw)
                sw.x -= col
                sw.y -= row
                sw.rebuild_raw_text()
                switches.append(sw)
        rooms = [Room(r.x - col, r.y - row, r.w, r.h)
                 for r in self._rooms_within(col, row, w, h)]
        return MapBlock(w, h, tiles, switches, rooms)

    def clear_rect(self, col: int, row: int, w: int, h: int, fill: int):
        """Set the rectangle's tiles to `fill`, and remove the switches on
        them and the rooms lying entirely inside it (i.e. everything that
        extract() of the same rectangle returns)."""
        for r in range(max(row, 0), min(row + h, self.height)):
            for c in range(max(col, 0), min(col + w, self.width)):
                self.data[r][c] = fill
        self.switches = [sw for sw in self.switches
                         if not (col <= sw.x < col + w and row <= sw.y < row + h)]
        removed = self._rooms_within(col, row, w, h)
        self.rooms = [r for r in self.rooms if not any(r is x for x in removed)]

    def paste(self, block: MapBlock, col: int, row: int,
              room_conflict: Callable[[Room, Room], bool] | None = None):
        """Paste `block` with its top-left tile at (col, row). Parts that
        fall outside the segment are dropped: tiles and switches off the
        data area, and rooms not fitting within the segment plus its
        wall ring. Existing switches on the overwritten tiles are
        replaced by the block's. Existing rooms for which
        `room_conflict(existing, pasted)` is true for some pasted room
        are removed (all existing rooms are kept if it is None)."""
        overwritten: set[tuple[int, int]] = set()
        for r, tile_row in enumerate(block.tiles):
            for c, tile in enumerate(tile_row):
                dc, dr = col + c, row + r
                if tile is not None and self._in_data(dc, dr):
                    self.data[dr][dc] = tile
                    overwritten.add((dc, dr))

        self.switches = [sw for sw in self.switches
                         if (sw.x, sw.y) not in overwritten]
        for sw in block.switches:
            if self._in_data(sw.x + col, sw.y + row):
                sw = copy.deepcopy(sw)
                sw.x += col
                sw.y += row
                sw.rebuild_raw_text()
                self.switches.append(sw)

        rooms = [Room(r.x + col, r.y + row, r.w, r.h) for r in block.rooms]
        rooms = [r for r in rooms if _room_fits(r, self.width, self.height)]
        if room_conflict is not None:
            self.rooms = [r for r in self.rooms
                          if not any(room_conflict(r, n) for n in rooms)]
        self.rooms.extend(rooms)
