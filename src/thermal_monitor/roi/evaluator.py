"""roi.evaluator -- snapshot-based ROI evaluation with stale-result guards."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

from thermal_monitor.roi import geometry as _g
from thermal_monitor.roi import measurements as _m
from thermal_monitor.roi.context import RoiActiveContext
from thermal_monitor.roi.enums import RoiCategory, RoiObjectType
from thermal_monitor.roi.errors import RoiStaleContextError
from thermal_monitor.roi.models import RoiDefinition, RoiMeasurement


@dataclass(slots=True)
class _QueueStats:
    submitted: int = 0
    evaluated: int = 0
    dropped_stale: int = 0
    dropped_superseded: int = 0
    last_duration_ms: float = 0.0


class RoiEvaluator:
    """Evaluates ROI snapshots against one immutable context.

    Latest-frame-wins: only the newest submitted frame is kept; older
    queued frames are counted as superseded, never processed. Results
    whose stamped context no longer matches the published context are
    discarded before delivery (never overwrite the new context).
    """

    def __init__(self, *, max_profile_samples: int = 2048) -> None:
        self._max_profile_samples = max_profile_samples
        self._pending: tuple[np.ndarray, int, float] | None = None
        self._stats = _QueueStats()

    @property
    def stats(self) -> _QueueStats:
        return self._stats

    def submit(self, image: np.ndarray, frame_id: int, frame_timestamp: float) -> None:
        self._pending = (np.asarray(image), int(frame_id), float(frame_timestamp))
        self._stats.submitted += 1

    def evaluate(self, rois: list[RoiDefinition], context: RoiActiveContext,
                 *, session_generation: int | None = None,
                 publish_context: RoiActiveContext | None = None) -> list[RoiMeasurement]:
        """Evaluate the latest pending frame; returns [] when nothing pending."""
        if self._pending is None:
            return []
        image, frame_id, frame_ts = self._pending
        self._pending = None
        if session_generation is None:
            session_generation = context.session_generation
        current = publish_context if publish_context is not None else context
        if (context.context_generation != current.context_generation
                or context.position_id != current.position_id
                or session_generation != current.session_generation):
            self._stats.dropped_stale += 1
            raise RoiStaleContextError("evaluation context was superseded before processing")
        t0 = time.perf_counter()
        results: list[RoiMeasurement] = []
        for roi in rois:
            if not roi.enabled or roi.category != RoiCategory.ANALYSIS:
                continue
            if (roi.camera_id != context.camera_id
                    or roi.ptz_id != context.ptz_id
                    or roi.position_id != context.position_id):
                continue
            results.append(self._evaluate_one(roi, image, frame_id, frame_ts, context))
        self._stats.evaluated += 1
        self._stats.last_duration_ms = (time.perf_counter() - t0) * 1000.0
        return results

    def _evaluate_one(self, roi: RoiDefinition, image: np.ndarray,
                      frame_id: int, frame_ts: float,
                      context: RoiActiveContext) -> RoiMeasurement:
        g = roi.geometry
        now = time.time()
        base = dict(roi_id=roi.roi_id, camera_id=roi.camera_id, ptz_id=roi.ptz_id,
                    position_id=roi.position_id,
                    context_generation=context.context_generation,
                    session_generation=context.session_generation,
                    frame_id=frame_id, frame_timestamp=frame_ts,
                    processed_at=now)
        t = roi.object_type
        if t == RoiObjectType.SPOT:
            out = _m.sample_spot(image, g)
            return RoiMeasurement(kind="spot", values={**out}, valid=out["valid"],
                                  error="" if out["valid"] else "no-data", **base)
        if t == RoiObjectType.HOTTEST_SPOT:
            out = _m.hottest_in_region(image, g.row1, g.col1, g.row2, g.col2)
            return RoiMeasurement(kind="hottest_spot", values={**out}, valid=out["valid"],
                                  error="" if out["valid"] else "no-data", **base)
        if t == RoiObjectType.COLDEST_SPOT:
            out = _m.coldest_in_region(image, g.row1, g.col1, g.row2, g.col2)
            return RoiMeasurement(kind="coldest_spot", values={**out}, valid=out["valid"],
                                  error="" if out["valid"] else "no-data", **base)
        if t == RoiObjectType.HOT_COLD_SPOTS:
            hot = _m.hottest_in_region(image, g.row1, g.col1, g.row2, g.col2)
            cold = _m.coldest_in_region(image, g.row1, g.col1, g.row2, g.col2)
            valid = bool(hot["valid"] or cold["valid"])
            return RoiMeasurement(kind="hot_cold_spots",
                                  values={"hot": hot, "cold": cold}, valid=valid,
                                  error="" if valid else "no-data", **base)
        if t in (RoiObjectType.FREE_LINE, RoiObjectType.HORIZONTAL_LINE,
                 RoiObjectType.VERTICAL_LINE, RoiObjectType.CROSS_LINE):
            if t == RoiObjectType.CROSS_LINE:
                gg = _g.LineGeometry(row1=g.center_row - g.half_length_row, col1=g.center_col,
                                     row2=g.center_row + g.half_length_row, col2=g.center_col)
            else:
                gg = g
            out = _m.line_profile(image, gg, max_samples=self._max_profile_samples)
            return RoiMeasurement(kind="line_profile", values={**out}, valid=out["valid"],
                                  error="" if out["valid"] else "no-data", **base)
        if t == RoiObjectType.POLYLINE:
            segs = []
            for a, b in zip(g.points, g.points[1:]):
                segs.append(_m.line_profile(
                    image, _g.LineGeometry(row1=a[0], col1=a[1], row2=b[0], col2=b[1]),
                    max_samples=self._max_profile_samples))
            valid = any(s["valid"] for s in segs)
            return RoiMeasurement(kind="polyline_profile", values={"segments": segs},
                                  valid=valid, error="" if valid else "no-data", **base)
        if t in (RoiObjectType.RECTANGLE, RoiObjectType.ELLIPSE, RoiObjectType.CIRCLE,
                 RoiObjectType.POLYGON):
            stats = _m.area_statistics(image, g)
            valid = stats.count > 0
            return RoiMeasurement(
                kind="area_stats",
                values={"min": stats.min, "max": stats.max, "mean": stats.mean,
                        "std": stats.std, "count": stats.count,
                        "hottest_row": stats.hottest_row, "hottest_col": stats.hottest_col,
                        "coldest_row": stats.coldest_row, "coldest_col": stats.coldest_col,
                        "method": "numpy_masked_stats"},
                valid=valid, error="" if valid else "no-data", **base)
        # Measurement/annotation objects produce geometry-only results.
        if t in (RoiObjectType.RULER,):
            return RoiMeasurement(kind="ruler", values=_m.ruler_distance(g),
                                  valid=True, **base)
        if t == RoiObjectType.HORIZONTAL_RULER:
            return RoiMeasurement(kind="ruler", values=_m.ruler_distance(g, axis="horizontal"),
                                  valid=True, **base)
        if t == RoiObjectType.VERTICAL_RULER:
            return RoiMeasurement(kind="ruler", values=_m.ruler_distance(g, axis="vertical"),
                                  valid=True, **base)
        if t == RoiObjectType.MEASURE_LINE:
            return RoiMeasurement(kind="measure_line", values=_m.ruler_distance(g),
                                  valid=True, **base)
        if t == RoiObjectType.MEASURE_ANGLE:
            return RoiMeasurement(kind="measure_angle", values=_m.measure_angle(g),
                                  valid=True, **base)
        return RoiMeasurement(kind="annotation", values={"geometry": g.to_dict()},
                              valid=True, **base)


__all__ = ["RoiEvaluator"]
