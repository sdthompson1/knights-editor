"""Automatic room placement for a segment.

Knights rooms are closed rectangles that include their own walls; adjacent
rooms share a wall line, and every passable tile must lie inside some room
(interior or non-corner edge). The dungeon generator surrounds each segment
with a one-tile wall ring, so rooms at the segment edge extend onto that
ring. Room coordinates in segment files are 1-based with the ring at 0 and
width+1 / height+1.

This module finds a *guillotine partition* of the segment-plus-ring grid:
a rectangle is either a single room or is cut by a full-length horizontal
or vertical line into two halves that share that line. Dynamic programming
over sub-rectangles finds the partition minimising a cost that prefers cut
lines on walls/doors over open floor, penalises rooms that contain several
separate floor areas (they should have been split), forbids oversized or
undersized rooms, and leaves solid wall blocks uncovered. The result is a
valid partition by construction (no overlapping interiors, all shared
edges); it is meant as a starting point for the user to adjust.
"""

from dataclasses import dataclass
from functools import lru_cache
import sys

from knights_editor.models import Segment, Room, TileEntry, entry_walking
from knights_editor.room_check import MAX_ROOM_W, MAX_ROOM_H

# Tile classes used by the algorithm.
WALL = 0    # walking blocked
DOOR = 1    # approach tile sitting in a one-tile gap between walls
FURN = 2    # other approach tiles (chests, switches, tables...)
FLOOR = 3   # walking clear


@dataclass
class AutoRoomsParams:
    room_cost: float = 1.0        # per room (discourages needless cuts)
    floor_cut: float = 3.0        # per open-floor tile a cut line crosses
    furn_cut: float = 1.0         # per furniture tile a cut line crosses
    component_cost: float = 4.0   # per extra separate floor area inside a room
    corner_cost: float = 6.0      # per passable tile at a cut endpoint (becomes
                                  # a room corner, which belongs to no room)
    max_w: int = MAX_ROOM_W       # max room size, walls included (the
    max_h: int = MAX_ROOM_H       # limits the room check enforces)
    max_floor_cuts: int = 4       # per rect and axis: how many cut lines that
                                  # cross open floor to consider (cheapest first)


def classify_grid(seg: Segment, tile_table: dict[int, TileEntry]) -> list[list[int]]:
    """Return the (height+2) x (width+2) grid of tile classes for the
    segment surrounded by its wall ring. Grid coords equal 1-based room
    coords: g[0][0] is the top-left ring tile, g[1][1] is data tile (0,0)."""
    W, H = seg.width, seg.height
    EW, EH = W + 2, H + 2
    raw = [[WALL] * EW for _ in range(EH)]
    for r in range(H):
        for c in range(W):
            acc = entry_walking(tile_table.get(seg.data[r][c]))
            raw[r + 1][c + 1] = {"blocked": WALL, "approach": FURN}.get(acc, FLOOR)

    g = [row[:] for row in raw]
    for r in range(EH):
        for c in range(EW):
            if raw[r][c] == FURN:
                lr = (c == 0 or raw[r][c - 1] == WALL) and (c == EW - 1 or raw[r][c + 1] == WALL)
                ud = (r == 0 or raw[r - 1][c] == WALL) and (r == EH - 1 or raw[r + 1][c] == WALL)
                if lr or ud:
                    g[r][c] = DOOR
    return g


def auto_rooms(seg: Segment, tile_table: dict[int, TileEntry],
               params: AutoRoomsParams | None = None,
               keep: list[Room] = ()) -> list[Room]:
    """Compute rooms covering the segment's passable tiles.

    If `keep` is given, those rooms are treated as fixed: the returned
    rooms (which do not include them) only cover what they leave
    uncovered, and may touch them only along shared edge lines."""
    P = params or AutoRoomsParams()
    g = classify_grid(seg, tile_table)
    EH, EW = len(g), len(g[0])

    # Fixed (kept) rooms: mark their tiles. Anything inside a kept room is
    # treated as wall for costing purposes (cuts through it are free and
    # it needs no covering); the two flags enforce non-overlap below.
    fixed_any = [[False] * EW for _ in range(EH)]       # any tile of a kept room
    fixed_interior = [[False] * EW for _ in range(EH)]  # strictly inside one
    for rm in keep:
        for r in range(max(0, rm.y), min(EH, rm.y + rm.h)):
            for c in range(max(0, rm.x), min(EW, rm.x + rm.w)):
                fixed_any[r][c] = True
                if rm.x < c < rm.x + rm.w - 1 and rm.y < r < rm.y + rm.h - 1:
                    fixed_interior[r][c] = True
    if keep:
        g = [[WALL if fixed_any[r][c] else g[r][c] for c in range(EW)]
             for r in range(EH)]

    # 2-D prefix sums so any line/rect count is O(1).
    def prefix(grid, pred):
        ps = [[0] * (EW + 1) for _ in range(EH + 1)]
        for r in range(EH):
            acc = 0
            for c in range(EW):
                acc += 1 if pred(grid[r][c]) else 0
                ps[r + 1][c + 1] = ps[r][c + 1] + acc
        return ps

    ps_pass = prefix(g, lambda v: v != WALL)   # passable tiles still needing a room
    ps_floor = prefix(g, lambda v: v == FLOOR)
    ps_furn = prefix(g, lambda v: v == FURN)
    ps_fixed_any = prefix(fixed_any, bool)
    ps_fixed_interior = prefix(fixed_interior, bool)

    def count(ps, x0, y0, x1, y1):
        """Count over the inclusive rect (x0,y0)-(x1,y1)."""
        return ps[y1 + 1][x1 + 1] - ps[y0][x1 + 1] - ps[y1 + 1][x0] + ps[y0][x0]

    def passable(x, y):
        return g[y][x] != WALL

    def components(x0, y0, x1, y1):
        """Number of 4-connected floor/furniture areas strictly inside the
        rect; door tiles separate areas."""
        seen = set()
        n = 0
        for r in range(y0 + 1, y1):
            for c in range(x0 + 1, x1):
                if g[r][c] in (FLOOR, FURN) and (r, c) not in seen:
                    n += 1
                    stack = [(r, c)]
                    seen.add((r, c))
                    while stack:
                        rr, cc = stack.pop()
                        for nr, nc in ((rr + 1, cc), (rr - 1, cc), (rr, cc + 1), (rr, cc - 1)):
                            if (y0 < nr < y1 and x0 < nc < x1 and (nr, nc) not in seen
                                    and g[nr][nc] in (FLOOR, FURN)):
                                seen.add((nr, nc))
                                stack.append((nr, nc))
        return n

    def vcut_cost(cx, y0, y1):
        """Cost of cutting along column cx between rows y0..y1 (endpoints
        become room corners)."""
        return (P.floor_cut * count(ps_floor, cx, y0 + 1, cx, y1 - 1)
                + P.furn_cut * count(ps_furn, cx, y0 + 1, cx, y1 - 1)
                + P.corner_cost * (passable(cx, y0) + passable(cx, y1)))

    def hcut_cost(cy, x0, x1):
        return (P.floor_cut * count(ps_floor, x0 + 1, cy, x1 - 1, cy)
                + P.furn_cut * count(ps_furn, x0 + 1, cy, x1 - 1, cy)
                + P.corner_cost * (passable(x0, cy) + passable(x1, cy)))

    sys.setrecursionlimit(max(sys.getrecursionlimit(), 10000))

    @lru_cache(maxsize=None)
    def solve(x0, y0, x1, y1):
        """Best (cost, rooms) for the closed rect (x0,y0)-(x1,y1)."""
        if count(ps_pass, x0, y0, x1, y1) == 0:
            return 0.0, ()  # solid wall: needs no room
        w, h = x1 - x0 + 1, y1 - y0 + 1

        best_cost, best_rooms = float('inf'), ()
        if (3 <= w <= P.max_w and 3 <= h <= P.max_h
                # may not overlap a kept room except along a shared edge line
                and count(ps_fixed_interior, x0, y0, x1, y1) == 0
                and count(ps_fixed_any, x0 + 1, y0 + 1, x1 - 1, y1 - 1) == 0):
            ncomp = components(x0, y0, x1, y1)
            best_cost = P.room_cost + P.component_cost * max(0, ncomp - 1)
            best_rooms = ((x0, y0, x1, y1),)

        for cut, cx in candidate_cuts([(vcut_cost(cx, y0, y1), cx) for cx in range(x0 + 1, x1)]):
            if cut >= best_cost:
                break
            ca, ra = solve(x0, y0, cx, y1)
            if cut + ca >= best_cost:
                continue
            cb, rb = solve(cx, y0, x1, y1)
            if cut + ca + cb < best_cost:
                best_cost, best_rooms = cut + ca + cb, ra + rb

        for cut, cy in candidate_cuts([(hcut_cost(cy, x0, x1), cy) for cy in range(y0 + 1, y1)]):
            if cut >= best_cost:
                break
            ca, ra = solve(x0, y0, x1, cy)
            if cut + ca >= best_cost:
                continue
            cb, rb = solve(x0, cy, x1, y1)
            if cut + ca + cb < best_cost:
                best_cost, best_rooms = cut + ca + cb, ra + rb

        return best_cost, best_rooms

    def candidate_cuts(cuts):
        """Cut lines worth trying, cheapest first: every line crossing no
        open floor, plus the `max_floor_cuts` cheapest others. Pruning the
        expensive lines keeps the search tractable on large segments."""
        cuts.sort()
        cheap = [c for c in cuts if c[0] < P.floor_cut]
        return cheap + [c for c in cuts if c[0] >= P.floor_cut][:P.max_floor_cuts]

    cost, rects = solve(0, 0, EW - 1, EH - 1)
    if cost == float('inf'):
        return []
    # Grid coords are already 1-based room coords (ring at 0).
    return [Room(x0, y0, x1 - x0 + 1, y1 - y0 + 1) for (x0, y0, x1, y1) in rects]
