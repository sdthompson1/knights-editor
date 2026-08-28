"""Validation of a segment's room rectangles against the rules the Knights
engine relies on (see room_map.cpp / view_manager.cpp):

- every passable tile must be inside some room (its interior, or an edge
  tile that is not a corner -- corners belong to no room);
- a passable edge tile (not a corner) must belong to exactly two rooms:
  the game picks which of the two to display from the knight's facing
  (e.g. on a horizontal boundary, the lower room when facing south);
- room corner tiles must be impassable: a knight standing on one would
  have four candidate rooms to display instead of two;
- two rooms may overlap only along a shared edge line, never in their
  interiors;
- a room needs at least one interior tile, i.e. must be at least 3x3;
- a room must be no larger than MAX_ROOM_W x MAX_ROOM_H, or the client's
  room view overflows the other UI elements;
- rooms lie within the segment plus its one-tile outer wall ring.

"Passable" means a knight could ever stand there (see entry_standable):
floor, but also doors, gates, pits and anything smashable. Homes,
switches, crystal balls and walls are not passable. The wall ring is
never passable.

Room coordinates are 1-based with the ring at 0 and width+1 / height+1.
"""

from dataclasses import dataclass
from enum import Enum, auto

from knights_editor.models import Segment, Room, TileEntry, entry_standable

# Largest room (including its edge squares) that the client can display
# properly. These match the largest rooms in the shipped Knights data.
MAX_ROOM_W = 10
MAX_ROOM_H = 11


class ProblemKind(Enum):
    TILE = auto()     # a single passable tile (marker: cross on the tile)
    ROOM = auto()     # a whole room (marker: outline of the room)
    OVERLAP = auto()  # the area two rooms share (marker: hatched fill)


@dataclass
class RoomProblem:
    message: str
    kind: ProblemKind
    # Where the problem is shown on the map, as a rectangle (x, y, w, h)
    # in room coordinates. Always lies within the segment plus its wall
    # ring, so it can be drawn and hovered: a room that reaches outside
    # the ring is clipped to it, and one entirely outside is shown as a
    # marker on the nearest ring squares (see _clamp).
    rect: tuple[int, int, int, int]

    def contains(self, col: int, row: int) -> bool:
        """True if the data tile (col, row) lies in the problem's rect."""
        x, y, w, h = self.rect
        return x <= col + 1 < x + w and y <= row + 1 < y + h


def problems_at(problems: list[RoomProblem], col: int, row: int) -> list[RoomProblem]:
    """The problems whose marker covers the data tile (col, row)."""
    return [p for p in problems if p.contains(col, row)]


def _fmt(r: Room) -> str:
    return f"({r.x}, {r.y}, {r.w}x{r.h})"


def _clamp(rect: tuple[int, int, int, int], seg: Segment) -> tuple[int, int, int, int]:
    """Clip a room-coordinate rect to the segment plus its wall ring
    (0..width+1, 0..height+1). A rect wholly outside collapses onto the
    nearest ring squares rather than vanishing; a degenerate (empty)
    rect is widened to at least one square."""
    x0, y0, w, h = rect
    x1, y1 = x0 + max(w, 1) - 1, y0 + max(h, 1) - 1
    cx0 = min(max(x0, 0), seg.width + 1)
    cx1 = min(max(x1, 0), seg.width + 1)
    cy0 = min(max(y0, 0), seg.height + 1)
    cy1 = min(max(y1, 0), seg.height + 1)
    return cx0, cy0, cx1 - cx0 + 1, cy1 - cy0 + 1


def _room_rect(r: Room) -> tuple[int, int, int, int]:
    return r.x, r.y, r.w, r.h


@dataclass
class TileCoverage:
    """How the rooms cover each data tile (col, row)."""
    # Number of rooms the tile belongs to (as an interior or non-corner
    # edge tile; corners do not count as belonging to the room).
    count: dict[tuple[int, int], int]
    # Tiles that are a non-corner edge tile of at least one room.
    edges: set[tuple[int, int]]
    # Tiles that are a corner of at least one room.
    corners: set[tuple[int, int]]


def tile_coverage(seg: Segment) -> TileCoverage:
    """Compute the room coverage of the segment's data tiles (ring tiles
    are ignored: they are never passable, so never matter)."""
    W, H = seg.width, seg.height
    count: dict[tuple[int, int], int] = {}
    edges: set[tuple[int, int]] = set()
    corners: set[tuple[int, int]] = set()
    for rm in seg.rooms:
        x0, y0 = rm.x, rm.y
        x1, y1 = rm.x + rm.w - 1, rm.y + rm.h - 1
        for ry in range(y0, y1 + 1):
            for rx in range(x0, x1 + 1):
                c, r = rx - 1, ry - 1
                if not (0 <= c < W and 0 <= r < H):
                    continue
                on_x, on_y = rx in (x0, x1), ry in (y0, y1)
                if on_x and on_y:
                    corners.add((c, r))
                    continue
                count[(c, r)] = count.get((c, r), 0) + 1
                if on_x or on_y:
                    edges.add((c, r))
    return TileCoverage(count, edges, corners)


def passable_tiles(seg: Segment, tile_table: dict[int, TileEntry]) -> set[tuple[int, int]]:
    """Data (col, row) of tiles a knight could ever stand on."""
    return {(c, r) for r in range(seg.height) for c in range(seg.width)
            if entry_standable(tile_table.get(seg.data[r][c]))}


def uncovered_tiles(seg: Segment, tile_table: dict[int, TileEntry]) -> list[tuple[int, int]]:
    """Data (col, row) of passable tiles not covered by any room."""
    cov = tile_coverage(seg)
    return sorted(t for t in passable_tiles(seg, tile_table) if cov.count.get(t, 0) == 0)


def room_overlap_rect(a: Room, b: Room) -> tuple[int, int, int, int] | None:
    """The rectangle (x, y, w, h) where the two rooms overlap illegally,
    i.e. their common area reaches into the interior of either room, or
    None if they do not. Sharing only edge/corner squares (e.g. adjacent
    rooms sharing a wall line) is allowed."""
    ix0, ix1 = max(a.x, b.x), min(a.x + a.w - 1, b.x + b.w - 1)
    iy0, iy1 = max(a.y, b.y), min(a.y + a.h - 1, b.y + b.h - 1)
    if ix0 > ix1 or iy0 > iy1:
        return None

    def hits_interior(r: Room) -> bool:
        return (ix0 < r.x + r.w - 1 and ix1 > r.x
                and iy0 < r.y + r.h - 1 and iy1 > r.y)
    if hits_interior(a) or hits_interior(b):
        return ix0, iy0, ix1 - ix0 + 1, iy1 - iy0 + 1
    return None


def rooms_overlap(a: Room, b: Room) -> bool:
    """True if the two rooms overlap illegally (see room_overlap_rect)."""
    return room_overlap_rect(a, b) is not None


def check_rooms(seg: Segment, tile_table: dict[int, TileEntry]) -> list[RoomProblem]:
    """Return all problems found with the segment's rooms (empty if none)."""
    problems: list[RoomProblem] = []
    W, H = seg.width, seg.height
    rooms = seg.rooms

    def room_problem(rm: Room, message: str):
        problems.append(RoomProblem(message, ProblemKind.ROOM, _clamp(_room_rect(rm), seg)))

    for rm in rooms:
        if rm.w < 3 or rm.h < 3:
            room_problem(rm, f"Room {_fmt(rm)} is smaller than 3x3")
        if rm.w > MAX_ROOM_W or rm.h > MAX_ROOM_H:
            room_problem(rm, f"Room {_fmt(rm)} is larger than {MAX_ROOM_W}x{MAX_ROOM_H}")
        if rm.x < 0 or rm.y < 0 or rm.x + rm.w - 1 > W + 1 or rm.y + rm.h - 1 > H + 1:
            room_problem(rm, f"Room {_fmt(rm)} extends outside the wall ring")

    for i, a in enumerate(rooms):
        for b in rooms[i + 1:]:
            rect = room_overlap_rect(a, b)
            if rect is not None:
                problems.append(RoomProblem(
                    f"Rooms {_fmt(a)} and {_fmt(b)} overlap",
                    ProblemKind.OVERLAP, _clamp(rect, seg)))

    cov = tile_coverage(seg)
    passable = passable_tiles(seg, tile_table)

    def tile_problem(c: int, r: int, message: str):
        problems.append(RoomProblem(message, ProblemKind.TILE, (c + 1, r + 1, 1, 1)))

    # Row-major order, so problems are listed in a predictable order.
    for c, r in sorted(passable, key=lambda t: (t[1], t[0])):
        n = cov.count.get((c, r), 0)
        if (c, r) in cov.corners:
            tile_problem(c, r, f"Tile ({c}, {r}) is a room corner but is passable")
        elif n == 0:
            tile_problem(c, r, f"Tile ({c}, {r}) is not in any room")
        elif (c, r) in cov.edges and n != 2:
            tile_problem(c, r, f"Tile ({c}, {r}) is on a room edge but is in "
                               f"{n} room{'s' if n != 1 else ''}, not 2")

    return problems
