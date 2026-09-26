"""On-screen text detection.

Uses the PP-OCRv3 DB text detector from OpenCV Zoo (Apache-2.0, 2.4 MB) through
``cv2.dnn.TextDetectionModel_DB``. We only need *where* text is, not what it
says, so no recognition model is loaded.

Text matters for reframing because overlays, captions, slides and title cards
are usually much wider than a 9:16 crop: cropping them cuts sentences in half.
The reframer uses these boxes to switch those moments to a "fit" layout.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger(__name__)

MODEL_NAME = "text_detection_en_ppocrv3_2023may.onnx"
MEAN = (122.67891434, 116.66876762, 104.00698793)


@dataclass(frozen=True)
class TextBox:
    """Axis-aligned text box in normalised [0, 1] frame coordinates."""

    x: float
    y: float
    w: float
    h: float


class TextDetector:
    def __init__(self, models_dir: Path) -> None:
        self._model = None
        self._size: tuple[int, int] | None = None
        path = models_dir / MODEL_NAME
        if not path.exists():
            log.warning("Text detection model missing (%s); text-aware framing disabled", path)
            return
        try:
            model = cv2.dnn.TextDetectionModel_DB(str(path))
            model.setBinaryThreshold(0.3)
            model.setPolygonThreshold(0.5)
            model.setMaxCandidates(100)
            model.setUnclipRatio(2.0)
            self._model = model
        except cv2.error as exc:  # pragma: no cover - depends on the OpenCV build
            log.warning("Could not load the text detector: %s", exc)

    @property
    def available(self) -> bool:
        return self._model is not None

    def detect(self, frame_bgr: np.ndarray) -> list[TextBox]:
        if self._model is None:
            return []
        h, w = frame_bgr.shape[:2]
        # DB needs input dims that are multiples of 32.
        size = (max(32, round(w / 32) * 32), max(32, round(h / 32) * 32))
        if size != self._size:
            self._model.setInputParams(1.0 / 255, size, MEAN, True)
            self._size = size
        polygons, _ = self._model.detect(frame_bgr)
        boxes = []
        for poly in polygons:
            x, y, bw, bh = cv2.boundingRect(np.asarray(poly, dtype=np.int32))
            boxes.append(TextBox(x / w, y / h, bw / w, bh / h))
        return boxes
