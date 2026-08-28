"""Shared zoom level handling for the map and tile palette widgets.

Zoom factors are relative to the default view: 1.0 ("100%") corresponds
to the standard 2x tile scaling.
"""

ZOOM_LEVELS = [0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0]


def next_level(current: float, step: int) -> float:
    """Return the zoom level `step` positions away from `current`."""
    idx = min(range(len(ZOOM_LEVELS)), key=lambda i: abs(ZOOM_LEVELS[i] - current))
    idx = max(0, min(len(ZOOM_LEVELS) - 1, idx + step))
    return ZOOM_LEVELS[idx]
