"""Turn raw input devices into clean relative events for the remote machine.

A plain mouse already speaks *relative* motion (``EV_REL``) plus button codes,
so it passes straight through.

A laptop **touchpad** is different: at the raw evdev level (below libinput) it
only reports *absolute* finger coordinates and tool/touch flags. None of the
nice gestures — tap-to-click, two-finger scroll — exist yet; libinput
synthesises them. Because Desk Connect grabs the device exclusively, it has to
do that synthesis itself. :class:`TouchpadProcessor` implements the essentials:

* single-finger motion  -> relative pointer motion (scaled to the pad size),
* quick light contact    -> left click (1 finger), right (2), middle (3),
* two-finger drag        -> scroll wheel,
* physical click-pad press (``BTN_LEFT``) passes through unchanged.
"""

from __future__ import annotations

import time

from .linux_input import (
    ABS_X,
    ABS_Y,
    BTN_LEFT,
    BTN_MIDDLE,
    BTN_RIGHT,
    BTN_TOOL_DOUBLETAP,
    BTN_TOOL_FINGER,
    BTN_TOOL_FIRST,
    BTN_TOOL_LAST,
    BTN_TOOL_TRIPLETAP,
    BTN_TOUCH,
    EV_ABS,
    EV_KEY,
    EV_REL,
    EV_SYN,
    REL_HWHEEL,
    REL_WHEEL,
    REL_X,
    REL_Y,
    SYN_REPORT,
    InputEvent,
)

# A full sweep across the pad should move roughly this many pixels per axis.
_TARGET_SPAN_X = 1920
_TARGET_SPAN_Y = 1080

# Gesture tuning (in post-scale pixel-ish units / seconds).
_TAP_MAX_SECONDS = 0.18
_TAP_MAX_TRAVEL = 12.0
_SCROLL_STEP = 24.0  # finger pixels per wheel notch


def make_pointer(has_abs: bool, x_range=None, y_range=None):
    """Return the right processor for a device."""
    if has_abs:
        return TouchpadProcessor(x_range, y_range)
    return MousePassthrough()


class MousePassthrough:
    """Relative mice need no processing."""

    def feed(self, ev: InputEvent) -> list[InputEvent]:
        return [ev]


class TouchpadProcessor:
    def __init__(self, x_range=None, y_range=None):
        self._sx = self._scale(x_range, _TARGET_SPAN_X)
        self._sy = self._scale(y_range, _TARGET_SPAN_Y)
        self._last_x: float | None = None
        self._last_y: float | None = None
        self._fingers = 0
        self._max_fingers = 0
        self._touch_start = 0.0
        self._travel = 0.0
        self._scroll_accum = 0.0
        self._hscroll_accum = 0.0

    @staticmethod
    def _scale(rng, target: int) -> float:
        if not rng:
            return 1.0
        span = rng[1] - rng[0]
        return target / span if span > 0 else 1.0

    def feed(self, ev: InputEvent) -> list[InputEvent]:
        if ev.type == EV_KEY:
            return self._key(ev)
        if ev.type == EV_ABS:
            if ev.code == ABS_X:
                return self._motion(0, ev.value)
            if ev.code == ABS_Y:
                return self._motion(1, ev.value)
            return []
        if ev.type == EV_SYN:
            return [ev]
        # EV_REL / EV_MSC etc. — pass through.
        return [ev]

    # -- buttons / tools ---------------------------------------------------
    def _key(self, ev: InputEvent) -> list[InputEvent]:
        code, val = ev.code, ev.value
        if code == BTN_TOUCH:
            if val == 1:
                self._begin_contact()
            else:
                return self._end_contact()
            return []
        if code == BTN_TOOL_FINGER:
            self._set_fingers(1, val)
            return []
        if code == BTN_TOOL_DOUBLETAP:
            self._set_fingers(2, val)
            return []
        if code == BTN_TOOL_TRIPLETAP:
            self._set_fingers(3, val)
            return []
        if BTN_TOOL_FIRST <= code <= BTN_TOOL_LAST:
            return []  # other tool flags are internal
        # Real buttons (physical click-pad press etc.) pass through.
        return [ev]

    def _begin_contact(self) -> None:
        self._touch_start = time.monotonic()
        self._travel = 0.0
        self._max_fingers = max(1, self._fingers)
        self._last_x = self._last_y = None
        self._scroll_accum = self._hscroll_accum = 0.0

    def _set_fingers(self, count: int, active: int) -> None:
        if active:
            self._fingers = count
            self._max_fingers = max(self._max_fingers, count)
            # Switching finger count: re-baseline so nothing jumps.
            self._last_x = self._last_y = None
        elif self._fingers == count:
            self._fingers = 0

    def _end_contact(self) -> list[InputEvent]:
        duration = time.monotonic() - self._touch_start
        was_tap = duration <= _TAP_MAX_SECONDS and self._travel <= _TAP_MAX_TRAVEL
        self._last_x = self._last_y = None
        out: list[InputEvent] = []
        if was_tap:
            button = {1: BTN_LEFT, 2: BTN_RIGHT, 3: BTN_MIDDLE}.get(self._max_fingers)
            if button is not None:
                out = self._click(button)
        self._max_fingers = 0
        return out

    @staticmethod
    def _click(button: int) -> list[InputEvent]:
        return [
            InputEvent(EV_KEY, button, 1),
            InputEvent(EV_SYN, SYN_REPORT, 0),
            InputEvent(EV_KEY, button, 0),
            InputEvent(EV_SYN, SYN_REPORT, 0),
        ]

    # -- motion / scroll ---------------------------------------------------
    def _motion(self, axis: int, value: int) -> list[InputEvent]:
        last = self._last_x if axis == 0 else self._last_y
        if axis == 0:
            self._last_x = value
        else:
            self._last_y = value
        if last is None:
            return []
        scale = self._sx if axis == 0 else self._sy
        delta = (value - last) * scale

        if self._fingers >= 2:
            return self._scroll(axis, delta)

        self._travel += abs(delta)
        idelta = int(round(delta))
        if idelta == 0:
            return []
        return [InputEvent(EV_REL, REL_X if axis == 0 else REL_Y, idelta)]

    def _scroll(self, axis: int, delta: float) -> list[InputEvent]:
        if axis == 1:  # vertical finger movement -> wheel
            self._scroll_accum += delta
            notches = int(self._scroll_accum / _SCROLL_STEP)
            if notches:
                self._scroll_accum -= notches * _SCROLL_STEP
                # finger down (positive) scrolls content down = wheel -1
                return [InputEvent(EV_REL, REL_WHEEL, -notches)]
        else:  # horizontal finger movement -> hwheel
            self._hscroll_accum += delta
            notches = int(self._hscroll_accum / _SCROLL_STEP)
            if notches:
                self._hscroll_accum -= notches * _SCROLL_STEP
                return [InputEvent(EV_REL, REL_HWHEEL, notches)]
        return []
