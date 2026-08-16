"""Aurum Vision live demo.

Three input modes so the presentation is not hostage to a webcam driver:

    webcam   default; live camera
    video    a recorded clip
    images   a folder of stills, advanced with the arrow keys or on a timer

Every mode renders the same dashboard and produces the same batch record, so
what is shown on stage is the same code path as the API.

Keys:  B new batch   S save batch   SPACE pause   Q/ESC quit
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import cv2

from app.batch import BatchSession
from app.dashboard import compose
from app.detector import DEFAULT_WEIGHTS, AurumDetector
from app.weight import get_weight_source

ROOT = Path(__file__).resolve().parent.parent
IMG_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
WINDOW = "Aurum Vision"


class FrameSource:
    """Uniform iterator over webcam / video / image-folder."""

    def __init__(self, mode: str, path: str | None, camera: int, width: int,
                 height: int, image_seconds: float) -> None:
        self.mode = mode
        self.image_seconds = image_seconds
        self._cap = None
        self._images: list[Path] = []
        self._idx = 0
        self._last_advance = 0.0

        if mode == "webcam":
            self._cap = cv2.VideoCapture(camera)
            if not self._cap.isOpened():
                raise RuntimeError(
                    f"Could not open camera {camera}. On macOS, grant camera "
                    f"permission to your terminal in System Settings > Privacy & "
                    f"Security > Camera. Or run with --mode images --path <folder>."
                )
            self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
            self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        elif mode == "video":
            if not path:
                raise RuntimeError("--mode video requires --path")
            self._cap = cv2.VideoCapture(path)
            if not self._cap.isOpened():
                raise RuntimeError(f"Could not open video {path}")
        elif mode == "images":
            if not path:
                raise RuntimeError("--mode images requires --path")
            p = Path(path)
            self._images = sorted(
                q for q in ([p] if p.is_file() else p.rglob("*"))
                if q.suffix.lower() in IMG_EXT
            )
            if not self._images:
                raise RuntimeError(f"No images found under {path}")
            self._last_advance = time.time()
        else:
            raise RuntimeError(f"unknown mode {mode!r}")

    @property
    def label(self) -> str:
        if self.mode == "images":
            return f"images {self._idx+1}/{len(self._images)}"
        return self.mode

    @property
    def scene_id(self) -> str:
        """Identifies the physical scene in view.

        The batch count is a median over a window of frames, which assumes the
        window covers one scene. That holds for a camera pointed at a bench, but
        in images mode each file is a different scene, so the window has to be
        cleared when the file changes or the median averages across unrelated
        photographs and collapses to zero.
        """
        return self._images[self._idx].name if self.mode == "images" else "live"

    def advance(self, delta: int) -> None:
        if self.mode == "images":
            self._idx = (self._idx + delta) % len(self._images)
            self._last_advance = time.time()

    def read(self):
        if self.mode == "images":
            if self.image_seconds > 0 and \
                    time.time() - self._last_advance > self.image_seconds:
                self.advance(1)
            return True, cv2.imread(str(self._images[self._idx]))
        ok, frame = self._cap.read()
        if not ok and self.mode == "video":  # loop the clip for a demo
            self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = self._cap.read()
        return ok, frame

    def release(self):
        if self._cap is not None:
            self._cap.release()


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", default="webcam", choices=["webcam", "video", "images"])
    ap.add_argument("--path", help="video file or image folder")
    ap.add_argument("--camera", type=int, default=0)
    ap.add_argument("--weights", default=str(DEFAULT_WEIGHTS))
    ap.add_argument("--conf", type=float, default=0.35)
    ap.add_argument("--iou", type=float, default=0.5)
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--window", type=int, default=45,
                    help="frames in the batch-count median window")
    ap.add_argument("--image-seconds", type=float, default=3.0,
                    help="auto-advance interval in images mode (0 = manual)")
    ap.add_argument("--weight-mode", default="auto",
                    choices=["auto", "hx711", "simulated", "off"])
    ap.add_argument("--hx711-port", default=None)
    ap.add_argument("--no-window", action="store_true",
                    help="headless: run N frames and print the batch record")
    ap.add_argument("--frames", type=int, default=60,
                    help="frames to process in --no-window mode")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    print("Loading Aurum Vision model…")
    det = AurumDetector(args.weights, conf=args.conf, iou=args.iou, imgsz=args.imgsz)
    det.warmup()
    print(f"  {det.model_version} — classes {det.classes}")

    src = FrameSource(args.mode, args.path, args.camera, args.width,
                      args.height, args.image_seconds)
    wsrc = get_weight_source(args.weight_mode, args.hx711_port)
    session = BatchSession(window=args.window, classes=det.classes)

    status, status_until = "LIVE", 0.0
    paused = False
    saved_path = None
    last_scene = None
    n = 0

    if not args.no_window:
        cv2.namedWindow(WINDOW, cv2.WINDOW_NORMAL)

    try:
        while True:
            if not paused:
                ok, frame = src.read()
                if not ok or frame is None:
                    print("frame source exhausted")
                    break
                scene = src.scene_id
                if scene != last_scene:
                    session.new_scene()
                    last_scene = scene
                result = det.predict(frame)
                session.add_frame(result.counts, result.mean_confidence)
                n += 1

            weight = wsrc.read().as_dict() if wsrc else None
            counts = session.stable_counts()
            record_preview = {"available": False}

            if status != "LIVE" and time.time() > status_until:
                status = "LIVE"

            canvas = compose(
                frame=frame, detections=result.detections, counts=counts,
                classes=det.classes, avg_conf=session.average_confidence(),
                fps=det.fps, model_version=det.model_version,
                batch_id=session.batch_id, frames=session.frames_seen,
                mode=src.label, status="PAUSED" if paused else status,
                weight=weight, recovery=record_preview,
            )

            if args.no_window:
                if n >= args.frames:
                    break
                continue

            cv2.imshow(WINDOW, canvas)
            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), 27):
                break
            elif key == ord("b"):
                session.reset()
                status, status_until = "NEW BATCH", time.time() + 1.5
                print(f"[batch] started {session.batch_id}")
            elif key == ord("s"):
                rec = session.record(det.model_version, weight, source=src.label)
                saved_path = session.save(rec)
                status, status_until = "SAVED", time.time() + 2.0
                print(f"[batch] saved {saved_path}")
                print(json.dumps(rec, indent=2))
            elif key == ord(" "):
                paused = not paused
            elif key in (81, ord(",")):
                src.advance(-1)
            elif key in (83, ord(".")):
                src.advance(1)
    finally:
        src.release()
        if not args.no_window:
            cv2.destroyAllWindows()

    rec = session.record(det.model_version,
                         wsrc.read().as_dict() if wsrc else None, source=src.label)
    print("\n=== BATCH COMPOSITION ===")
    print(json.dumps(rec, indent=2))
    if args.no_window:
        session.save(rec)
    return 0


if __name__ == "__main__":
    sys.exit(main())
