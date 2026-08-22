"""The Aurum Vision dashboard — OpenCV rendering of the live view.

Layout follows the deck: camera on the left, detection summary on the right,
status strip along the bottom. Drawn with OpenCV rather than a web stack so the
demo is one process with no browser, no server and no network dependency —
the failure modes that ruin live presentations.
"""

from __future__ import annotations

import cv2
import numpy as np

# Aurum palette (BGR).
BG = (18, 16, 14)
PANEL = (30, 27, 24)
RULE = (58, 54, 48)
TEXT = (238, 238, 240)
MUTED = (150, 148, 145)
GOLD = (60, 175, 214)
GREEN = (110, 200, 120)
RED = (70, 70, 225)
AMBER = (60, 180, 245)

CLASS_COLORS = {
    "PCB": (120, 200, 90),
    "RAM": (214, 175, 60),
    "CPU": (90, 150, 245),
    "Connector": (200, 120, 220),
}

PANEL_W = 340
BAR_H = 54
FONT = cv2.FONT_HERSHEY_SIMPLEX


def _text(img, s, org, scale=0.5, color=TEXT, thick=1):
    cv2.putText(img, s, org, FONT, scale, color, thick, cv2.LINE_AA)


def draw_detections(frame: np.ndarray, detections) -> np.ndarray:
    """Boxes + `CLASS 0.94` labels, drawn on a copy of the frame."""
    out = frame.copy()
    for d in detections:
        x1, y1, x2, y2 = d.xyxy
        color = CLASS_COLORS.get(d.cls, GOLD)
        cv2.rectangle(out, (x1, y1), (x2, y2), color, 2)

        label = f"{d.cls} {d.conf:.2f}"
        (tw, th), base = cv2.getTextSize(label, FONT, 0.55, 2)
        ty = max(y1, th + 6)
        cv2.rectangle(out, (x1, ty - th - 6), (x1 + tw + 10, ty + base - 2), color, -1)
        _text(out, label, (x1 + 5, ty - 2), 0.55, (20, 20, 20), 2)
    return out


def _summary_panel(
    h: int,
    counts: dict,
    classes: list[str],
    total: int,
    avg_conf: float,
    batch_id: str,
    frames: int,
    weight: dict | None,
    recovery: dict | None,
) -> np.ndarray:
    p = np.full((h, PANEL_W, 3), PANEL, dtype=np.uint8)
    y = 34
    _text(p, "DETECTION SUMMARY", (18, y), 0.52, GOLD, 1)
    y += 10
    cv2.line(p, (18, y), (PANEL_W - 18, y), RULE, 1)
    y += 30

    for c in classes:
        n = counts.get(c, 0)
        col = CLASS_COLORS.get(c, GOLD)
        cv2.rectangle(p, (18, y - 11), (28, y - 1), col, -1)
        _text(p, c, (38, y), 0.52, TEXT if n else MUTED, 1)
        _text(p, str(n), (PANEL_W - 46, y), 0.62, TEXT if n else MUTED, 2)
        y += 30

    y += 6
    cv2.line(p, (18, y), (PANEL_W - 18, y), RULE, 1)
    y += 28
    _text(p, "Objects", (18, y), 0.5, MUTED)
    _text(p, str(total), (PANEL_W - 46, y), 0.55, TEXT, 2)
    y += 26
    _text(p, "Avg Conf.", (18, y), 0.5, MUTED)
    _text(p, f"{avg_conf * 100:.1f}%" if total else "--", (PANEL_W - 66, y), 0.55, TEXT, 2)
    y += 34

    cv2.line(p, (18, y), (PANEL_W - 18, y), RULE, 1)
    y += 28
    _text(p, "BATCH", (18, y), 0.48, GOLD)
    y += 24
    _text(p, batch_id, (18, y), 0.5, TEXT)
    y += 22
    _text(p, f"{frames} frames observed", (18, y), 0.42, MUTED)
    y += 32

    if weight:
        cv2.line(p, (18, y), (PANEL_W - 18, y), RULE, 1)
        y += 26
        if weight.get("simulated"):
            _text(p, "SIMULATED SENSOR", (18, y), 0.46, AMBER, 1)
        else:
            _text(p, "MEASURED WEIGHT", (18, y), 0.46, GREEN, 1)
        y += 26
        _text(p, f"{weight['kg']:.3f} kg", (18, y), 0.72, TEXT, 2)
        y += 30

    if recovery is not None:
        cv2.line(p, (18, y), (PANEL_W - 18, y), RULE, 1)
        y += 24
        _text(p, "MATERIAL ESTIMATE", (18, y), 0.44, MUTED)
        y += 20
        if not recovery.get("available", False):
            # The reference data *is* loaded; the estimate is blocked because a
            # detected class has no cited figure. Saying "not loaded" would
            # misattribute a fail-closed refusal to a missing file.
            _text(p, "NO CITED DATA", (18, y), 0.44, AMBER)
            y += 18
            _text(p, "FOR THIS BATCH", (18, y), 0.44, AMBER)
        else:
            for metal, agg in sorted(recovery.get("material_estimate", {}).items()):
                _text(p, f"{metal} ~{agg['typical_g']:.4f} g", (18, y), 0.46, GOLD, 1)
                y += 18
            _text(p, "ESTIMATE — NOT AN ASSAY", (18, y), 0.40, AMBER)

    # Controls, pinned to the bottom of the panel.
    cy = h - 92
    cv2.line(p, (18, cy), (PANEL_W - 18, cy), RULE, 1)
    cy += 22
    for key, desc in (("B", "new batch"), ("S", "save batch"), ("Q", "quit")):
        _text(p, f"[{key}]", (18, cy), 0.44, GOLD)
        _text(p, desc, (56, cy), 0.44, MUTED)
        cy += 20
    return p


def _status_bar(
    w: int, model_version: str, fps: float, mode: str, status: str, status_color
) -> np.ndarray:
    bar = np.full((BAR_H, w, 3), PANEL, dtype=np.uint8)
    cv2.line(bar, (0, 0), (w, 0), RULE, 1)
    _text(bar, "Model", (24, 22), 0.42, MUTED)
    _text(bar, model_version, (24, 42), 0.5, TEXT)

    _text(bar, "FPS", (300, 22), 0.42, MUTED)
    _text(bar, f"{fps:.1f}", (300, 42), 0.5, TEXT)

    _text(bar, "Mode", (400, 22), 0.42, MUTED)
    _text(bar, mode.upper(), (400, 42), 0.5, TEXT)

    _text(bar, "Status", (w - 190, 22), 0.42, MUTED)
    cv2.circle(bar, (w - 182, 37), 5, status_color, -1)
    _text(bar, status, (w - 168, 42), 0.5, status_color)
    return bar


def compose(
    frame: np.ndarray,
    detections,
    counts: dict,
    classes: list[str],
    avg_conf: float,
    fps: float,
    model_version: str,
    batch_id: str,
    frames: int,
    mode: str,
    status: str = "LIVE",
    weight: dict | None = None,
    recovery: dict | None = None,
    header: bool = True,
) -> np.ndarray:
    """Assemble the full dashboard frame."""
    vis = draw_detections(frame, detections)
    h, w = vis.shape[:2]

    head_h = 64 if header else 0
    canvas = np.full((head_h + h + BAR_H, w + PANEL_W, 3), BG, dtype=np.uint8)

    if header:
        cv2.rectangle(canvas, (0, 0), (w + PANEL_W, head_h), PANEL, -1)
        _text(canvas, "AURUM VISION", (24, 30), 0.78, GOLD, 2)
        _text(canvas, "E-WASTE COMPONENT IDENTIFICATION", (24, 50), 0.44, MUTED)
        cv2.line(canvas, (0, head_h - 1), (w + PANEL_W, head_h - 1), RULE, 1)

    canvas[head_h : head_h + h, 0:w] = vis

    total = sum(counts.values())
    panel = _summary_panel(h, counts, classes, total, avg_conf, batch_id, frames, weight, recovery)
    canvas[head_h : head_h + h, w : w + PANEL_W] = panel

    color = GREEN if status == "LIVE" else (AMBER if status == "SAVED" else RED)
    canvas[head_h + h :, :] = _status_bar(w + PANEL_W, model_version, fps, mode, status, color)
    return canvas
