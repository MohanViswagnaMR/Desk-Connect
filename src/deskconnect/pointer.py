"""Normalise pointer input so it replays cleanly on the remote machine.

A plain mouse emits *relative* motion (``EV_REL``) which we forward verbatim.
A laptop **touchpad** emits *absolute* coordinates (``EV_ABS``) plus touch
tooling events — those cannot be replayed on a virtual relative pointer, which
is why "the keyboard works but the mouse doesn't" on laptops.

:class:`PointerNormalizer` converts a device's absolute motion into relative
deltas (scaled so a full swipe across the pad ≈ a screen width), resets its
baseline when the finger lifts so re-touching never makes the cursor jump, and
drops touch-tooling buttons. Relative devices pass straight through.
"""

from __future__ import annotations

from .linux_input import (
    ABS_X,
    ABS_Y,
    BTN_TOOL_FIRST,
    BTN_TOOL_LAST,
    BTN_TOUCH,
    EV_ABS,
    EV_KEY,
    EV_REL,
    REL_X,
    REL_Y,
    InputEvent,
)

# A full sweep across the pad should move roughly this many pixels per axis.
_TARGET_SPAN_X = 1920
_TARGET_SPAN_Y = 1080


class PointerNormalizer:
    def __init__(
        self,
        x_range: tuple[int, int] | None = None,
        y_range: tuple[int, int] | None = None,
    ):
        self._scale_x = self._scale_for(x_range, _TARGET_SPAN_X)
        self._scale_y = self._scale_for(y_range, _TARGET_SPAN_Y)
        self._last_x: float | None = None
        self._last_y: float | None = None

    @staticmethod
    def _scale_for(rng: tuple[int, int] | None, target: int) -> float:
        if not rng:
            return 1.0
        span = rng[1] - rng[0]
        return target / span if span > 0 else 1.0

    def feed(self, ev: InputEvent) -> list[InputEvent]:
        if ev.type == EV_ABS:
            if ev.code == ABS_X:
                return self._abs_delta(0, ev.value)
            if ev.code == ABS_Y:
                return self._abs_delta(1, ev.value)
            return []  # pressure / multitouch / etc. — ignored
        if ev.type == EV_KEY:
            if ev.code == BTN_TOUCH:
                if ev.value == 0:  # finger lifted → forget baseline
                    self._last_x = self._last_y = None
                return []
            if BTN_TOOL_FIRST <= ev.code <= BTN_TOOL_LAST:
                return []  # BTN_TOOL_FINGER/PEN/... are touchpad-internal
            return [ev]  # real buttons (left/right/middle) pass through
        return [ev]  # EV_REL, EV_SYN, EV_MSC pass through unchanged

    def _abs_delta(self, axis: int, value: int) -> list[InputEvent]:
        last = self._last_x if axis == 0 else self._last_y
        if axis == 0:
            self._last_x = value
        else:
            self._last_y = value
        if last is None:
            return []  # establishing the baseline; no motion yet
        scale = self._scale_x if axis == 0 else self._scale_y
        delta = int(round((value - last) * scale))
        if delta == 0:
            return []
        return [InputEvent(EV_REL, REL_X if axis == 0 else REL_Y, delta)]
