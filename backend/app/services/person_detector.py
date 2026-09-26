"""Person detection (whole body, including arms and hands).

Faces tell us *who* to follow; a person box tells us *how wide* they are, so a
gesturing hand or an object held at arm's length isn't cropped off. Uses
NanoDet-Plus (OpenCV Zoo, Apache-2.0, 3.8 MB, COCO classes). Only the
``person`` class is kept. Pre/post-processing follows the OpenCV Zoo reference
implementation (letterbox to 416x416, distribution-focal box decoding, NMS).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger(__name__)

MODEL_NAME = "object_detection_nanodet_2022nov.onnx"
INPUT = 416
REG_MAX = 7
MEAN = np.array([103.53, 116.28, 123.675], dtype=np.float32).reshape(1, 1, 3)
STD = np.array([57.375, 57.12, 58.395], dtype=np.float32).reshape(1, 1, 3)
PERSON_CLASS = 0


@dataclass(frozen=True)
class PersonBox:
    """Normalised [0, 1] box of one person."""

    x: float
    y: float
    w: float
    h: float
    score: float


class PersonDetector:
    def __init__(self, models_dir: Path, score_threshold: float = 0.4, iou_threshold: float = 0.5) -> None:
        self._net = None
        self._score = score_threshold
        self._iou = iou_threshold
        path = models_dir / MODEL_NAME
        if not path.exists():
            log.warning("Person detection model missing (%s); body-aware framing disabled", path)
            return
        try:
            self._net = cv2.dnn.readNet(str(path))
        except cv2.error as exc:  # pragma: no cover - depends on the OpenCV build
            log.warning("Could not load the person detector: %s", exc)
            return
        self._project = np.arange(REG_MAX + 1, dtype=np.float32)
        self._anchors: dict[int, np.ndarray] = {}

    def _anchor_grid(self, stride: int) -> np.ndarray:
        if stride not in self._anchors:
            n = INPUT // stride
            xv, yv = np.meshgrid(np.arange(n) * stride, np.arange(n) * stride)
            centre = 0.5 * (stride - 1)
            self._anchors[stride] = np.column_stack((xv.ravel() + centre, yv.ravel() + centre))
        return self._anchors[stride]

    @property
    def available(self) -> bool:
        return self._net is not None

    def detect(self, frame_bgr: np.ndarray) -> list[PersonBox]:
        if self._net is None:
            return []
        h, w = frame_bgr.shape[:2]
        # Letterbox into a 416x416 square so people aren't squashed.
        scale = INPUT / max(w, h)
        nw, nh = int(round(w * scale)), int(round(h * scale))
        pad_x, pad_y = (INPUT - nw) // 2, (INPUT - nh) // 2
        canvas = np.zeros((INPUT, INPUT, 3), np.uint8)
        canvas[pad_y : pad_y + nh, pad_x : pad_x + nw] = cv2.resize(frame_bgr, (nw, nh), interpolation=cv2.INTER_AREA)
        blob = cv2.dnn.blobFromImage((canvas.astype(np.float32) - MEAN) / STD)
        self._net.setInput(blob)
        outs = [o.reshape(-1, o.shape[-1]) for o in self._net.forward(self._net.getUnconnectedOutLayersNames())]
        # Output order differs between OpenCV builds; pair class and box heads by level size.
        classes = sorted((o for o in outs if o.shape[1] != 4 * (REG_MAX + 1)), key=len, reverse=True)
        regs = sorted((o for o in outs if o.shape[1] == 4 * (REG_MAX + 1)), key=len, reverse=True)

        boxes, scores = [], []
        for cls, reg in zip(classes, regs):
            stride = INPUT // int(round(np.sqrt(len(cls))))
            anchors = self._anchor_grid(stride)
            person = cls[:, PERSON_CLASS]
            keep = person >= self._score
            if not keep.any():
                continue
            dist = np.exp(reg.reshape(-1, REG_MAX + 1))
            dist = (dist / dist.sum(axis=1, keepdims=True)) @ self._project
            dist = dist.reshape(-1, 4)[keep] * stride
            pts = anchors[keep]
            x1 = np.clip(pts[:, 0] - dist[:, 0], 0, INPUT)
            y1 = np.clip(pts[:, 1] - dist[:, 1], 0, INPUT)
            x2 = np.clip(pts[:, 0] + dist[:, 2], 0, INPUT)
            y2 = np.clip(pts[:, 1] + dist[:, 3], 0, INPUT)
            boxes.extend(np.column_stack([x1, y1, x2 - x1, y2 - y1]).tolist())
            scores.extend(person[keep].tolist())
        if not boxes:
            return []

        out = []
        for i in np.array(cv2.dnn.NMSBoxes(boxes, scores, self._score, self._iou)).ravel():
            bx, by, bw, bh = boxes[i]
            x0, y0 = (bx - pad_x) / scale / w, (by - pad_y) / scale / h
            out.append(PersonBox(
                float(max(0.0, x0)), float(max(0.0, y0)),
                float(min(1.0, bw / scale / w)), float(min(1.0, bh / scale / h)), float(scores[i]),
            ))
        return out
