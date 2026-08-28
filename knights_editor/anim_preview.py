"""Animated preview of kts.Anim objects.

Replays the frame sequences the game engine actually generates from an
Anim (see src/shared/anim.hpp and engine/impl/creature.cpp in the game):
melee attack, throw, parry, crossbow states, walking, the zombie "just
hit" recoil, and the vampire bat's random wing-flap. Weapon overlays are
composited using the mod's kts.SetOverlayOffsets table, and timings are
taken from a selectable ItemType (backswing/downswing times etc.), so a
mod author can check how an Anim will look in the game without loading
it.

The engine's animation model is a single primitive - "show frame F until
time T, then revert to frame 0" - so every sequence here is a list of
(frame, duration) steps. Bodies are never displaced by attacks; only the
weapon overlay moves (up to a full square on the downswing).
"""

import random
from dataclasses import dataclass, field

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QToolButton,
    QButtonGroup, QSizePolicy,
)
from PySide6.QtGui import QPainter, QColor, QPen
from PySide6.QtCore import Qt, QSize, QTimer, QElapsedTimer, QRect

from knights_editor.mod_loader import KtsObject, KtsGraphic

TILE = 16          # dungeon square size in source pixels
SCALE = 4          # preview zoom
CANVAS_SQUARES = 3

# Frame indices, as in AnimFrame (game's shared/anim.hpp). The Lua table
# is frame-major with facing (N,E,S,W) as the fast index.
AF_NORMAL = 0
AF_BACKSWING = 1
AF_IMPACT = 2
AF_PARRY = 3
AF_THROW_BACK = 4
AF_THROW_DOWN = 5
AF_XBOW = 6
AF_XBOW_LOAD = 7

# Pseudo-frames for vampire bat mode (anim { ..., bat=true }): the four
# table entries are four frames of a single facing-less animation.
BAT_FLAP = -1      # random one of entries 1-3, changing every 260 ms
BAT_BITE = -2      # entry 4

FACING_LABELS = ["North", "East", "South", "West"]
_FACING_VEC = [(0, -1), (1, 0), (0, 1), (-1, 0)]

_FRAME_NAMES = ["Normal", "Melee backswing", "Melee downswing", "Parry",
                "Throw backswing", "Throw follow-through",
                "Holding crossbow", "Reloading crossbow"]
_BAT_FRAME_NAMES = ["Flap 1", "Flap 2", "Flap 3", "Bite"]


def frame_label(frame: int, n_frames: int, bat: bool = False) -> str:
    """Display name of one anim frame row. `n_frames` is the number of
    frames the anim defines (its entry count / 4); frame 3 is the parry
    for knight-sized anims but the "just hit" recoil for monsters."""
    if bat:
        return _BAT_FRAME_NAMES[frame] if frame < 4 else f"Frame {frame + 1}"
    if frame == AF_PARRY and n_frames <= 4:
        return "Just hit / parry"
    if frame < len(_FRAME_NAMES):
        return _FRAME_NAMES[frame]
    return f"Frame {frame + 1}"


# Fallback overlay offsets (the base module's kts.SetOverlayOffsets
# values), used if the loaded mods never made the call. Indexed by
# frame*4 + facing; entries are (overlay graphic direction, x, y) with
# offsets in 1/1000ths of a square.
_BASE_OVERLAY_OFFSETS = [
    (3, -250, -188), (0, 188, -250), (1, 250, 188), (2, -188, 250),
    (2, 188, 625), (3, -625, 188), (0, -188, -625), (1, 625, -188),
    (0, 0, -1000), (1, 1000, 0), (2, 0, 1000), (3, -1000, 0),
    (3, -250, -125), (0, 125, -250), (1, 250, 125), (2, -125, 250),
    (2, 188, 625), (3, -625, 188), (0, -188, -625), (1, 625, -188),
]
N_OVERLAY_FRAMES = 5   # frames 0-4 draw an overlay (game's N_OVERLAY_FRAME)


@dataclass
class Step:
    frame: int
    duration: int              # ms
    overlay: bool = True       # draw the weapon overlay during this step


@dataclass
class Sequence:
    name: str
    steps: list = field(default_factory=list)
    moving: bool = False       # glide across the canvas while playing
    ms_per_square: int = 420

    @property
    def total(self) -> int:
        return max(1, sum(s.duration for s in self.steps))


def eval_number(value, default: int) -> int:
    """A numeric ItemType/MonsterType property: either a plain number or
    a Lua function returning one (e.g. rng_time_range closures, which
    the loader's kts.RandomRange stub makes deterministic)."""
    if isinstance(value, bool) or value is None:
        return default
    if isinstance(value, (int, float)):
        return int(value)
    try:
        result = value()
        if isinstance(result, (int, float)):
            return int(result)
    except Exception:
        pass
    return default


def build_sequences(n_frames: int, bat: bool, t: dict,
                    crossbow: bool = False) -> list[Sequence]:
    """The sequences the game can generate from an anim with `n_frames`
    frames (entry count / 4). `t` holds evaluated timings in ms (see
    AnimPreviewPanel._timings). `crossbow` means the selected weapon is
    crossbow-like (has a reload_time): the game then keeps the knight in
    the "holding crossbow" frame while walking or standing."""
    if bat:
        msq = t['bat_ms_per_square']
        return [
            Sequence("Fly", [Step(BAT_FLAP, 3 * msq, False)],
                     moving=True, ms_per_square=msq),
            Sequence("Hover", [Step(BAT_FLAP, 2080, False)]),
            Sequence("Bite", [Step(BAT_FLAP, 520, False),
                              Step(BAT_BITE, t['melee_delay'], False)]),
        ]

    seqs = []
    if n_frames == 1:
        # Missile anim: frame 0 only, facing = direction of flight.
        msq = t['missile_ms_per_square']
        seqs.append(Sequence("Flight", [Step(AF_NORMAL, 3 * msq, False)],
                             moving=True, ms_per_square=msq))
        seqs.append(Sequence("Static", [Step(AF_NORMAL, 1000, False)]))
        return seqs

    if n_frames >= 3:
        seqs.append(Sequence("Melee attack", [
            Step(AF_BACKSWING, t['melee_bs']),
            Step(AF_IMPACT, t['melee_ds']),
            Step(AF_NORMAL, t['melee_delay'] + 460),
        ]))
    if n_frames >= 6:
        seqs.append(Sequence("Throw", [
            Step(AF_THROW_BACK, t['missile_bs']),
            Step(AF_THROW_DOWN, t['missile_ds'], overlay=False),
            Step(AF_NORMAL, 600),
        ]))
    if n_frames >= 8:
        seqs.append(Sequence("Parry", [
            Step(AF_PARRY, t['parry_delay']),
            Step(AF_NORMAL, 860),
        ]))
        seqs.append(Sequence("Crossbow reload", [
            Step(AF_XBOW_LOAD, t['reload'], overlay=False),
            Step(AF_XBOW, 1200, overlay=False),
        ]))
    elif n_frames >= 4:
        seqs.append(Sequence("Hit recoil", [
            Step(AF_PARRY, t['stun']),
            Step(AF_NORMAL, 600),
        ]))
    msq = t['walk_ms_per_square']
    idle_frame = AF_XBOW if crossbow and n_frames >= 8 else AF_NORMAL
    seqs.append(Sequence("Walk", [Step(idle_frame, 3 * msq)],
                         moving=True, ms_per_square=msq))
    seqs.append(Sequence("Stand", [Step(idle_frame, 1000)]))
    return seqs


class _AnimCanvas(QWidget):
    """The drawing surface: a 3x3-square patch of floor with the current
    frame (plus weapon overlay) composited on it by the owning panel."""

    def __init__(self, panel: 'AnimPreviewPanel'):
        super().__init__(panel)
        self._panel = panel
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def sizeHint(self) -> QSize:
        side = CANVAS_SQUARES * TILE * SCALE + 2
        return QSize(side, side)

    def paintEvent(self, event):
        sq = TILE * SCALE
        side = CANVAS_SQUARES * sq
        painter = QPainter(self)
        painter.setClipRect(QRect(1, 1, side, side))
        light, dark = QColor(72, 72, 72), QColor(58, 58, 58)
        for gy in range(CANVAS_SQUARES):
            for gx in range(CANVAS_SQUARES):
                painter.fillRect(1 + gx * sq, 1 + gy * sq, sq, sq,
                                 light if (gx + gy) % 2 == 0 else dark)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform,
                              False)
        self._panel.paint_scene(painter)
        painter.setClipping(False)
        painter.setPen(QPen(QColor(90, 90, 90), 1))
        painter.drawRect(0, 0, side + 1, side + 1)
        painter.end()


class AnimPreviewPanel(QWidget):
    """Playback controls + canvas for one Anim: sequence, facing and
    weapon (ItemType) selectors, with the weapon supplying both the
    overlay graphics and the attack timings."""

    def __init__(self, anim: KtsObject, inspector, gfx_cache, parent=None):
        super().__init__(parent)
        self._anim = anim
        self._ins = inspector
        self._cache = gfx_cache
        self._frames = anim.graphics()
        self._bat = bool(anim.props.get('bat'))
        self._n_frames = 1 if self._bat else max(1, len(self._frames) // 4)
        self._offsets = getattr(inspector.loader, 'overlay_offsets', None) \
            or _BASE_OVERLAY_OFFSETS
        self._misc = self._misc_config()
        self._monster = self._find_monster()
        self._weapons = self._scan_weapons()
        self._sequences: list[Sequence] = []
        self._flap_slot = -1
        self._flap_choice = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)

        controls = QHBoxLayout()
        controls.setSpacing(6)
        self._seq_combo = QComboBox()
        self._seq_combo.setToolTip("Animation sequence to play")
        controls.addWidget(self._seq_combo)
        facing_row = QHBoxLayout()
        facing_row.setSpacing(0)
        self._facing_group = QButtonGroup(self)
        self._facing_group.setExclusive(True)
        for i, label in enumerate(FACING_LABELS):
            btn = QToolButton()
            btn.setText(label[0])
            btn.setCheckable(True)
            btn.setFixedSize(24, 24)
            if self._bat:
                btn.setToolTip(f"Fly {label.lower()} "
                               "(bat sprites ignore facing)")
            else:
                btn.setToolTip(f"Face {label.lower()}")
            self._facing_group.addButton(btn, i)
            facing_row.addWidget(btn)
        self._facing_group.button(2).setChecked(True)   # south: facing viewer
        controls.addLayout(facing_row)
        controls.addStretch()
        layout.addLayout(controls)

        # Second row: weapon selector and transport controls (the single
        # row is too wide for a narrow sidebar).
        controls = QHBoxLayout()
        controls.setSpacing(6)
        self._weapon_combo = QComboBox()
        self._weapon_combo.setToolTip(
            "ItemType supplying the weapon overlay and the attack timings")
        self._weapon_combo.addItem("(no weapon)", None)
        for name, item in self._weapons:
            self._weapon_combo.addItem(name, id(item))
        self._weapon_combo.setCurrentIndex(self._default_weapon_index() + 1)
        if self._bat or not self._weapons:
            self._weapon_combo.hide()
        controls.addWidget(self._weapon_combo)
        self._step_back = QToolButton()
        self._step_back.setArrowType(Qt.ArrowType.LeftArrow)
        self._step_back.setToolTip("Previous animation step (pauses)")
        self._step_back.setAutoRaise(True)
        self._step_back.clicked.connect(lambda: self._step(-1))
        controls.addWidget(self._step_back)
        self._pause_btn = QToolButton()
        self._pause_btn.setText("❚❚")
        self._pause_btn.setToolTip("Pause / resume")
        self._pause_btn.setAutoRaise(True)
        self._pause_btn.setCheckable(True)
        self._pause_btn.toggled.connect(self._on_pause)
        controls.addWidget(self._pause_btn)
        self._step_fwd = QToolButton()
        self._step_fwd.setArrowType(Qt.ArrowType.RightArrow)
        self._step_fwd.setToolTip("Next animation step (pauses)")
        self._step_fwd.setAutoRaise(True)
        self._step_fwd.clicked.connect(lambda: self._step(1))
        controls.addWidget(self._step_fwd)
        controls.addStretch()
        layout.addLayout(controls)

        canvas_row = QHBoxLayout()
        self._canvas = _AnimCanvas(self)
        canvas_row.addWidget(self._canvas)
        canvas_row.addStretch()
        layout.addLayout(canvas_row)

        self._info = QLabel()
        self._info.setStyleSheet("color: gray; font-size: 8pt;")
        self._info.setWordWrap(True)
        layout.addWidget(self._info)

        self._clock = QElapsedTimer()
        self._clock.start()
        self._base_ms = 0
        self._playing = True
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._canvas.update)

        self._rebuild_sequences()
        self._seq_combo.currentIndexChanged.connect(self._on_seq_changed)
        self._facing_group.idClicked.connect(
            lambda _id: self._canvas.update())
        self._weapon_combo.currentIndexChanged.connect(self._on_weapon_changed)

    # --- state scanning -------------------------------------------------

    def _namespaces(self):
        spaces = list(self._ins.namespaces())
        spaces.append(("_G", self._ins.globals_table()))
        return spaces

    def _misc_config(self) -> dict:
        """Numeric entries of kts.MISC_CONFIG, if the mod set it."""
        out = {}
        try:
            cfg = self._ins.loader.lua.globals().kts['MISC_CONFIG']
            for k, v in cfg.items():
                if isinstance(k, str) and isinstance(v, (int, float)) \
                        and not isinstance(v, bool):
                    out[k] = int(v)
        except Exception:
            pass
        return out

    def _find_monster(self) -> KtsObject | None:
        """The MonsterType using this anim, if any (for its speed and
        default weapon)."""
        for _, ns in self._namespaces():
            try:
                entries = list(ns.items())
            except Exception:
                continue
            for _, v in entries:
                if isinstance(v, KtsObject) and v.kind == 'MonsterType' \
                        and v.props.get('anim') is self._anim:
                    return v
        return None

    def _scan_weapons(self) -> list[tuple[str, KtsObject]]:
        """ItemTypes that carry an overlay or any attack timing, sorted
        by name."""
        keys = ('overlay', 'melee_backswing_time', 'missile_backswing_time',
                'reload_time', 'missile_speed')
        found = {}
        for _, ns in self._namespaces():
            try:
                entries = list(ns.items())
            except Exception:
                continue
            for k, v in entries:
                if not (isinstance(k, str) and isinstance(v, KtsObject)
                        and v.kind == 'ItemType'):
                    continue
                if id(v) in found or not any(v.props.get(p) is not None
                                             for p in keys):
                    continue
                found[id(v)] = (self._ins.global_name(v) or k, v)
        return sorted(found.values(), key=lambda nv: nv[0].lower())

    def _default_weapon_index(self) -> int:
        """Index into self._weapons for the initial selection (-1 for no
        weapon): the item that throws/shoots this anim as its missile,
        else the owning monster's weapon, else the sword."""
        for i, (_, item) in enumerate(self._weapons):
            if item.props.get('missile_anim') is self._anim:
                return i
        if self._monster is not None:
            weapon = self._monster.props.get('weapon')
            for i, (_, item) in enumerate(self._weapons):
                if item is weapon:
                    return i
        for i, (name, item) in enumerate(self._weapons):
            if name.split('.')[-1] == 'i_sword':
                return i
        for i, (_, item) in enumerate(self._weapons):
            if item.props.get('overlay') is not None:
                return i
        return -1

    def _current_weapon(self) -> KtsObject | None:
        wid = self._weapon_combo.currentData()
        if wid is None:
            return None
        for _, item in self._weapons:
            if id(item) == wid:
                return item
        return None

    # --- timing ---------------------------------------------------------

    def _timings(self) -> dict:
        misc = self._misc
        walk_time = misc.get('walk_time', 420)
        w = self._current_weapon()
        p = w.props if w is not None else {}
        speed = 100
        if self._monster is not None:
            speed = eval_number(self._monster.props.get('speed'), 100)
        missile_speed = eval_number(p.get('missile_speed'), 250)
        return {
            'melee_bs': eval_number(p.get('melee_backswing_time'), 280),
            'melee_ds': eval_number(p.get('melee_downswing_time'), 280),
            'missile_bs': eval_number(p.get('missile_backswing_time'), 280),
            'missile_ds': eval_number(p.get('missile_downswing_time'), 280),
            'reload': eval_number(p.get('reload_time'), 3640),
            # A hit creature's recoil lasts for the attacker's stun time,
            # or 1000 ms (walking_monster_damage_delay) if unknown.
            'stun': eval_number(
                p.get('melee_stun_time'),
                misc.get('walking_monster_damage_delay', 1000)),
            'melee_delay': misc.get('melee_delay_time', 140),
            'parry_delay': misc.get('parry_delay', 140),
            'walk_ms_per_square': max(1, walk_time * 100 // max(1, speed)),
            'missile_ms_per_square':
                max(1, walk_time * 100 // max(1, missile_speed)),
            'bat_ms_per_square': max(1, walk_time * 100 // max(1, speed)),
        }

    # --- control handling ----------------------------------------------

    def _rebuild_sequences(self):
        previous = self._seq_combo.currentText()
        weapon = self._current_weapon()
        crossbow = weapon is not None \
            and weapon.props.get('reload_time') is not None
        self._sequences = build_sequences(self._n_frames, self._bat,
                                          self._timings(), crossbow=crossbow)
        self._seq_combo.blockSignals(True)
        self._seq_combo.clear()
        self._seq_combo.addItems([s.name for s in self._sequences])
        if previous:
            idx = self._seq_combo.findText(previous)
            if idx >= 0:
                self._seq_combo.setCurrentIndex(idx)
        self._seq_combo.blockSignals(False)
        self._restart()

    def _sequence(self) -> Sequence | None:
        idx = self._seq_combo.currentIndex()
        if 0 <= idx < len(self._sequences):
            return self._sequences[idx]
        return None

    def _restart(self):
        self._base_ms = 0
        self._clock.restart()
        seq = self._sequence()
        steppable = seq is not None and len(seq.steps) >= 2
        self._step_back.setEnabled(steppable)
        self._step_fwd.setEnabled(steppable)
        self._update_info()
        self._canvas.update()

    def _step(self, delta: int):
        """Jump to the start of the previous/next step of the sequence,
        pausing playback."""
        seq = self._sequence()
        if seq is None or len(seq.steps) < 2:
            return
        if self._playing:
            self._pause_btn.setChecked(True)   # triggers _on_pause(True)
        t = self._now() % seq.total
        starts = []
        acc = 0
        for s in seq.steps:
            starts.append(acc)
            acc += s.duration
        idx = len(seq.steps) - 1
        for i, s in enumerate(seq.steps):
            if t < starts[i] + s.duration:
                idx = i
                break
        self._base_ms = starts[(idx + delta) % len(seq.steps)]
        self._canvas.update()

    def _on_seq_changed(self, _index):
        self._restart()

    def _on_weapon_changed(self, _index):
        self._rebuild_sequences()

    def _on_pause(self, paused: bool):
        if paused:
            self._base_ms = self._now()
            self._playing = False
            self._timer.stop()
            self._pause_btn.setText("▶")
        else:
            self._clock.restart()
            self._playing = True
            self._timer.start()
            self._pause_btn.setText("❚❚")

    def _update_info(self):
        seq = self._sequence()
        if seq is None:
            self._info.setText("")
            return
        parts = []
        for step in seq.steps:
            if step.frame == BAT_FLAP:
                name = "flap (random, 260 ms/frame)"
            elif step.frame == BAT_BITE:
                name = "bite"
            else:
                name = frame_label(step.frame, self._n_frames).lower()
            parts.append(f"{name} {step.duration} ms")
        text = " → ".join(parts)
        if seq.moving:
            text += f"  ({seq.ms_per_square} ms/square)"
        self._info.setText(text)

    def _now(self) -> int:
        return self._base_ms + (self._clock.elapsed() if self._playing else 0)

    def showEvent(self, event):
        super().showEvent(event)
        if self._playing:
            self._clock.restart()
            self._timer.start()

    def hideEvent(self, event):
        super().hideEvent(event)
        if self._playing:
            self._base_ms = self._now()
            self._clock.restart()
        self._timer.stop()

    # --- rendering ------------------------------------------------------

    def _pixmap(self, graphic):
        if self._cache is None or not isinstance(graphic, KtsGraphic):
            return None
        pm = self._cache.get_graphic_pixmap(graphic.to_model())
        return pm if not pm.isNull() else None

    def _body_graphic(self, frame: int, facing: int):
        if self._bat:
            if frame == BAT_BITE:
                idx = 3
            else:
                # As the game client: pick one of the three flap
                # graphics at random, changing every 260 ms.
                slot = self._now() // 260
                if slot != self._flap_slot:
                    self._flap_slot = slot
                    self._flap_choice = random.randrange(3)
                idx = self._flap_choice
        else:
            idx = frame * 4 + facing
        if 0 <= idx < len(self._frames):
            return self._frames[idx]
        return None

    def paint_scene(self, painter: QPainter):
        seq = self._sequence()
        if seq is None or not self._frames:
            return
        t = self._now() % seq.total
        step = seq.steps[-1]
        remaining = t
        for s in seq.steps:
            if remaining < s.duration:
                step = s
                break
            remaining -= s.duration

        facing = max(0, self._facing_group.checkedId())
        sq = TILE * SCALE
        # Base position: the centre square, displaced along the facing
        # when the sequence glides (wrapping across the canvas).
        cx, cy = 1 + sq, 1 + sq
        if seq.moving:
            progress = (t % seq.total) / seq.ms_per_square - 1.5
            vx, vy = _FACING_VEC[facing]
            cx += round(progress * sq) * vx
            cy += round(progress * sq) * vy

        body = self._body_graphic(step.frame, facing)
        pm = self._pixmap(body)
        if pm is not None:
            hx, hy = body.handle_pos
            painter.drawPixmap(
                QRect(cx - hx * SCALE, cy - hy * SCALE,
                      pm.width() * SCALE, pm.height() * SCALE), pm)

        # Weapon overlay, drawn in front of the body (as the game does).
        # Frames 5+ never draw one: the item has left the hand (throw
        # follow-through) or is part of the body sprite (crossbow).
        if not step.overlay or self._bat \
                or not (0 <= step.frame < N_OVERLAY_FRAMES):
            return
        weapon = self._current_weapon()
        overlay = weapon.props.get('overlay') if weapon is not None else None
        if not isinstance(overlay, KtsObject):
            return
        ov_graphics = overlay.graphics()
        entry_idx = step.frame * 4 + facing
        if entry_idx >= len(self._offsets):
            return
        dir_idx, ofsx, ofsy = self._offsets[entry_idx]
        if dir_idx >= len(ov_graphics):
            return
        og = ov_graphics[dir_idx]
        opm = self._pixmap(og)
        if opm is None:
            return
        ohx, ohy = og.handle_pos
        painter.drawPixmap(
            QRect(cx + round(ofsx * sq / 1000) - ohx * SCALE,
                  cy + round(ofsy * sq / 1000) - ohy * SCALE,
                  opm.width() * SCALE, opm.height() * SCALE), opm)
