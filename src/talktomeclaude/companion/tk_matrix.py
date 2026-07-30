"""Dependency-free Matrix Deck renderer for the native Tk companion."""

from __future__ import annotations

import math
import random
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from talktomeclaude.companion.contracts import CompanionSnapshot
from talktomeclaude.core import RuntimePhase


@dataclass(frozen=True, slots=True)
class MatrixVisual:
    accent: str
    accent_dim: str
    speed: float


_GREEN = MatrixVisual("#76ffb4", "#1eff78", 2.0)
_IDLE = MatrixVisual("#6ceb9c", "#2a9a5e", 0.55)
_BLUE = MatrixVisual("#7cc6ff", "#3686ff", 1.25)
_AMBER = MatrixVisual("#ffd166", "#e89b2c", 1.0)
_RED = MatrixVisual("#ff7070", "#dc3a3a", 0.8)
_VIOLET = MatrixVisual("#c9a7ff", "#8b5cf6", 0.8)


def matrix_visual(phase: RuntimePhase) -> MatrixVisual:
    if phase is RuntimePhase.RECORDING:
        return _GREEN
    if phase in {
        RuntimePhase.TRANSCRIBING,
        RuntimePhase.DELIVERING,
        RuntimePhase.WAITING_FOR_CLAUDE,
        RuntimePhase.PLANNING,
    }:
        return _BLUE
    if phase in {RuntimePhase.AWAITING_CONFIRMATION, RuntimePhase.SPEAKING}:
        return _AMBER
    if phase is RuntimePhase.PAUSED:
        return _VIOLET
    if phase in {RuntimePhase.DISCONNECTED, RuntimePhase.RECOVERABLE_ERROR}:
        return _RED
    return _IDLE


def microphone_label(snapshot: CompanionSnapshot) -> str:
    if snapshot.runtime.phase is not RuntimePhase.RECORDING:
        return "MIC · STANDBY"
    level = max(0.0, min(1.0, float(snapshot.microphone_level)))
    if level < 0.02:
        return "MIC · NO SIGNAL"
    return f"MIC · LIVE {round(level * 100):.0f}%"


@dataclass(slots=True)
class _RainDrop:
    column: int
    y: float
    speed: float
    characters: list[str]


class MatrixDeck:
    """Render state, signal history, and deterministic Matrix rain on a Canvas."""

    WIDTH = 520
    HEIGHT = 290
    _RAIN_LEFT = 20
    _RAIN_TOP = 52
    _RAIN_RIGHT = 224
    _RAIN_BOTTOM = 260
    _RAIN_STEP = 13
    _CHARACTERS = tuple(
        "ｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄﾅﾆﾇﾈﾉﾊﾋﾌﾍﾎABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:<>*+-="
    )

    def __init__(self, canvas: Any, *, random_seed: int = 1984) -> None:
        self._canvas = canvas
        self._random = random.Random(random_seed)
        self._snapshot: CompanionSnapshot | None = None
        self._waveform: deque[float] = deque([0.0] * 36, maxlen=36)
        self._scan_phase = 0.0
        self._frame = 0
        self._last_active = time.monotonic()
        columns = (self._RAIN_RIGHT - self._RAIN_LEFT) // self._RAIN_STEP
        initial_columns = list(range(columns))
        self._random.shuffle(initial_columns)
        self._drops = [
            self._new_drop(
                columns,
                random_y=True,
                column=initial_columns[index % len(initial_columns)],
            )
            for index in range(10)
        ]

    def _new_drop(
        self,
        columns: int,
        *,
        random_y: bool = False,
        column: int | None = None,
    ) -> _RainDrop:
        length = self._random.randint(7, 15)
        y = -float(length * self._RAIN_STEP)
        if random_y:
            y = self._random.uniform(self._RAIN_TOP, self._RAIN_BOTTOM)
        return _RainDrop(
            column=(
                self._random.randrange(max(1, columns))
                if column is None
                else column
            ),
            y=y,
            speed=self._random.uniform(0.45, 1.45),
            characters=[self._random.choice(self._CHARACTERS) for _ in range(length)],
        )

    def set_snapshot(self, snapshot: CompanionSnapshot) -> None:
        self._snapshot = snapshot
        level = (
            snapshot.microphone_level
            if snapshot.runtime.phase is RuntimePhase.RECORDING
            else 0.0
        )
        normalized = max(0.0, min(1.0, float(level)))
        if normalized > 0.0 and max(self._waveform, default=0.0) == 0.0:
            self._waveform = deque(
                (
                    normalized
                    * (0.34 + 0.66 * abs(math.sin(index * 0.71)))
                    for index in range(36)
                ),
                maxlen=36,
            )
        else:
            self._waveform.append(normalized)
        if snapshot.runtime.phase is not RuntimePhase.IDLE or level > 0.0:
            self._last_active = time.monotonic()
        self._draw()

    def tick(self) -> None:
        if self._snapshot is None:
            return
        self._frame += 1
        eco = (
            self._snapshot.runtime.phase is RuntimePhase.IDLE
            and time.monotonic() - self._last_active > 60.0
        )
        if eco and self._frame % 20:
            return
        visual = matrix_visual(self._snapshot.runtime.phase)
        speed = visual.speed * (0.25 if eco else 1.0)
        self._scan_phase += 0.035 * speed
        columns = (self._RAIN_RIGHT - self._RAIN_LEFT) // self._RAIN_STEP
        for index, drop in enumerate(self._drops):
            drop.y += drop.speed * self._RAIN_STEP * 0.36 * speed
            if not eco and self._random.random() < 0.025:
                position = self._random.randrange(len(drop.characters))
                drop.characters[position] = self._random.choice(self._CHARACTERS)
            tail = drop.y - len(drop.characters) * self._RAIN_STEP
            if tail > self._RAIN_BOTTOM:
                self._drops[index] = self._new_drop(
                    columns,
                    column=index % max(1, columns),
                )
        self._draw()

    def _rounded_box(
        self,
        left: float,
        top: float,
        right: float,
        bottom: float,
        radius: float,
        **options: object,
    ) -> None:
        points = (
            left + radius,
            top,
            right - radius,
            top,
            right,
            top,
            right,
            top + radius,
            right,
            bottom - radius,
            right,
            bottom,
            right - radius,
            bottom,
            left + radius,
            bottom,
            left,
            bottom,
            left,
            bottom - radius,
            left,
            top + radius,
            left,
            top,
        )
        self._canvas.create_polygon(
            *points,
            smooth=True,
            splinesteps=24,
            **options,
        )

    def _draw(self) -> None:
        snapshot = self._snapshot
        if snapshot is None:
            return
        canvas = self._canvas
        visual = matrix_visual(snapshot.runtime.phase)
        canvas.delete("all")
        canvas.create_rectangle(
            0,
            0,
            self.WIDTH,
            self.HEIGHT,
            fill="#010403",
            outline="",
        )
        self._rounded_box(
            6,
            6,
            self.WIDTH - 6,
            self.HEIGHT - 6,
            20,
            fill="#031008",
            outline=visual.accent_dim,
            width=1,
        )
        self._rounded_box(
            9,
            9,
            self.WIDTH - 9,
            self.HEIGHT - 9,
            18,
            fill="#06160e",
            outline="#123d28",
            width=1,
        )
        self._rounded_box(
            self._RAIN_LEFT,
            self._RAIN_TOP,
            self._RAIN_RIGHT,
            self._RAIN_BOTTOM,
            14,
            fill="#03100a",
            outline="#174d31",
            width=1,
        )
        canvas.create_text(
            20,
            18,
            text="TALKTOMEJOHNNY",
            anchor="nw",
            fill="#e9fff2",
            font=("Bahnschrift SemiBold", 13, "bold"),
            tags=("title",),
        )

        scan_y = self._RAIN_TOP + (
            (math.sin(self._scan_phase) + 1.0)
            * 0.5
            * (self._RAIN_BOTTOM - self._RAIN_TOP)
        )
        canvas.create_line(
            self._RAIN_LEFT + 2,
            scan_y,
            self._RAIN_RIGHT - 2,
            scan_y,
            fill="#155b37",
            width=1,
            tags=("rain",),
        )
        for drop in self._drops:
            x = self._RAIN_LEFT + 7 + drop.column * self._RAIN_STEP
            for offset, character in enumerate(drop.characters):
                y = drop.y - offset * self._RAIN_STEP
                if self._RAIN_TOP + 6 <= y <= self._RAIN_BOTTOM - 4:
                    canvas.create_text(
                        x,
                        y,
                        text=character,
                        anchor="n",
                        fill="#f5fff8" if offset == 0 else visual.accent_dim,
                        font=("Cascadia Mono", 8, "bold"),
                        tags=("rain",),
                    )

        info_left = 252
        canvas.create_text(
            info_left,
            86,
            text="BRIDGE  TERMINAL VOICE",
            anchor="nw",
            fill="#8fbea1",
            font=("Cascadia Mono", 8),
            tags=("telemetry",),
        )
        canvas.create_text(
            info_left,
            103,
            text="ROUTE   PROVIDER NEUTRAL",
            anchor="nw",
            fill="#8fbea1",
            font=("Cascadia Mono", 8),
            tags=("telemetry",),
        )

        mic_level = (
            max(0.0, min(1.0, snapshot.microphone_level))
            if snapshot.runtime.phase is RuntimePhase.RECORDING
            else 0.0
        )
        volume_level = 0.0 if snapshot.output_muted else snapshot.output_volume / 100
        self._draw_meter(138, mic_level, visual)
        self._draw_meter(162, volume_level, visual)

        self._rounded_box(
            info_left,
            186,
            500,
            257,
            10,
            fill="#082018",
            outline="#1d5b3b",
            width=1,
        )
        points: list[float] = []
        center = 221.5
        step = 248 / max(1, len(self._waveform) - 1)
        for index, level in enumerate(self._waveform):
            taper = 1.0 - abs((index - 17.5) / 18.0)
            polarity = math.sin(index * 1.35 + self._scan_phase * 3.0)
            points.extend(
                (248 + index * step, center + polarity * 30 * level * taper)
            )
        canvas.create_line(
            *points,
            fill="#174d34",
            width=9,
            smooth=True,
            tags=("waveform",),
        )
        canvas.create_line(
            *points,
            fill=visual.accent_dim,
            width=4,
            smooth=True,
            tags=("waveform",),
        )
        canvas.create_line(
            *points,
            fill=visual.accent,
            width=1,
            smooth=True,
            tags=("waveform",),
        )
        footer = {
            RuntimePhase.RECORDING: "Live input · finish when ready",
            RuntimePhase.TRANSCRIBING: "Speech recognition is running locally",
            RuntimePhase.WAITING_FOR_CLAUDE: "Transcript delivered · awaiting assistant",
            RuntimePhase.SPEAKING: "Spoken reply · record again to interrupt",
            RuntimePhase.AWAITING_CONFIRMATION: "Review required · no transcript shown here",
        }.get(snapshot.runtime.phase, "Local voice companion · provider neutral")
        canvas.create_text(
            info_left,
            266,
            text=footer,
            anchor="nw",
            fill="#8fbea1",
            font=("Segoe UI", 8),
            tags=("footer",),
        )

    def _draw_meter(
        self,
        y: int,
        ratio: float,
        visual: MatrixVisual,
    ) -> None:
        left, top, width = 413, y + 1, 84
        self._rounded_box(
            left,
            top,
            left + width,
            top + 9,
            4,
            fill="#102b1d",
            outline="",
        )
        fill_width = width * max(0.0, min(1.0, ratio))
        if fill_width >= 2:
            self._rounded_box(
                left,
                top,
                left + fill_width,
                top + 9,
                min(4, fill_width / 2),
                fill=visual.accent_dim,
                outline="",
            )


__all__ = ["MatrixDeck", "MatrixVisual", "matrix_visual", "microphone_label"]
