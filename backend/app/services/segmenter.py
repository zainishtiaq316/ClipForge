"""Automatic segmentation of the full video into clips.

The whole timeline is covered with back-to-back clips close to the requested
target length. Cut points are chosen from natural boundaries, in order of
preference:

* **pauses in speech** (see ``analyzer.detect_pauses``): cutting mid-sentence is the
  most noticeable mistake an auto-clipper can make;
* **scene cuts**: an edit in the source is already a clean visual boundary;
* the exact target length, only when neither exists within the allowed window.

Each candidate is scored by how natural it is minus how far it lands from the
target length, and the best one in ``[min, max]`` wins (greedy, left to right).
"""

from __future__ import annotations

from dataclasses import dataclass

MIN_CLIP_S = 3.0


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    reason: str  # why the clip ends here: "pause" | "scene" | "length" | "end"


@dataclass(frozen=True)
class _Candidate:
    t: float
    quality: float  # higher = more natural boundary
    reason: str


SCENE_QUALITY = 0.8
PAUSE_QUALITY = 1.5
PAUSE_AT_SCENE_BONUS = 0.7  # a pause that coincides with an edit is the ideal cut
COINCIDENCE_S = 0.6


def _candidates(scene_cuts: list[float], pauses: list[tuple[float, float]]) -> list[_Candidate]:
    # B-roll edits often happen mid-sentence, so a scene cut alone is a weaker
    # boundary than a pause in speech.
    out = [_Candidate(t, SCENE_QUALITY, "scene") for t in scene_cuts]
    for start, end in pauses:
        # Longer pauses are stronger boundaries (end of a thought vs. a breath).
        quality = PAUSE_QUALITY + min(end - start, 1.5)
        if any(start - COINCIDENCE_S <= c <= end + COINCIDENCE_S for c in scene_cuts):
            quality += PAUSE_AT_SCENE_BONUS
        out.append(_Candidate(round((start + end) / 2, 3), quality, "pause"))
    return sorted(out, key=lambda c: c.t)


def segment(
    duration: float,
    scene_cuts: list[float],
    pauses: list[tuple[float, float]],
    target: float = 30.0,
) -> list[Segment]:
    if duration <= 0:
        return []
    target = max(MIN_CLIP_S * 2, target)
    lo, hi = target * 0.6, target * 1.5
    if duration <= hi:
        return [Segment(0.0, round(duration, 3), "end")]

    candidates = _candidates(scene_cuts, pauses)
    segments: list[Segment] = []
    start = 0.0
    while duration - start > hi:
        window = [c for c in candidates if start + lo <= c.t <= start + hi]
        if window:
            # Normalised distance from target costs up to ~1.5 quality points.
            best = max(window, key=lambda c: c.quality - 1.5 * abs(c.t - start - target) / target)
            end, reason = best.t, best.reason
        else:
            end, reason = start + target, "length"
        segments.append(Segment(round(start, 3), round(end, 3), reason))
        start = end

    remainder = duration - start
    if segments and remainder < lo:
        # A tiny trailing clip is useless: absorb it into the previous one.
        last = segments.pop()
        segments.append(Segment(last.start, round(duration, 3), "end"))
    else:
        segments.append(Segment(round(start, 3), round(duration, 3), "end"))
    return segments
