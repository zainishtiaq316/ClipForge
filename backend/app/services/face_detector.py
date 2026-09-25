"""Face detection.

Primary detector is YuNet (OpenCV Zoo, MIT licence): a ~230 KB CNN that is far
more robust than Haar cascades to profile views, small faces and poor lighting,
and still runs in ~15 ms per 640px frame on a laptop CPU. If the model can't be
loaded (e.g. an unusual OpenCV build), we fall back to the Haar cascade that
ships inside ``opencv-python`` so the app keeps working, just less accurately.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")

import cv2  # noqa: E402  (log level must be set before import)
import numpy as np  # noqa: E402

log = logging.getLogger(__name__)

# Newest first: the 2026 export has dynamic input dims (required by OpenCV 5's
# ONNX engine); the 2023 export works with OpenCV 4.x.
YUNET_MODELS = ("face_detection_yunet_2026may.onnx", "face_detection_yunet_2023mar.onnx")


@dataclass(frozen=True)
class Face:
    """A detected face in normalised [0, 1] frame coordinates."""

    cx: float
    cy: float
    w: float
    h: float
    score: float

    @property
    def area(self) -> float:
        return self.w * self.h


class FaceDetector:
    def __init__(self, models_dir: Path, score_threshold: float = 0.6) -> None:
        self._score_threshold = score_threshold
        self._size: tuple[int, int] | None = None
        self._yunet = None
        self._haar = None

        for name in YUNET_MODELS:
            path = models_dir / name
            if not path.exists():
                continue
            try:
                detector = cv2.FaceDetectorYN.create(str(path), "", (320, 320), score_threshold, 0.3, 50)
                detector.detect(np.zeros((320, 320, 3), dtype=np.uint8))  # smoke test
                self._yunet = detector
                self.backend = f"yunet:{name}"
                break
            except cv2.error as exc:
                log.warning("Could not load %s: %s", name, exc)

        if self._yunet is None:
            cascade = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
            self._haar = cv2.CascadeClassifier(str(cascade))
            self.backend = "haar"
            log.warning("YuNet unavailable, falling back to Haar cascade face detection")

    def detect(self, frame_bgr: np.ndarray) -> list[Face]:
        h, w = frame_bgr.shape[:2]
        if self._yunet is not None:
            if self._size != (w, h):
                self._yunet.setInputSize((w, h))
                self._size = (w, h)
            _, rows = self._yunet.detect(frame_bgr)
            if rows is None:
                return []
            faces = []
            for x, y, bw, bh, *rest in rows:
                faces.append(
                    Face(float((x + bw / 2) / w), float((y + bh / 2) / h), float(bw / w), float(bh / h), float(rest[-1]))
                )
            return faces

        gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
        rects = self._haar.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6, minSize=(24, 24))
        return [
            Face(float((x + bw / 2) / w), float((y + bh / 2) / h), float(bw / w), float(bh / h), 0.7)
            for x, y, bw, bh in rects
        ]
