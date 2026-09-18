"""halcon.adapter -- snapshot-based HALCON execution boundary.

1. Receive an immutable image snapshot (copied by the caller).
2. Verify dimensions/type/finite data.
3. Convert geometry via halcon.geometry.
4. Execute on the calling worker thread (never the GUI thread).
5. Return typed RoiMeasurement values with full context metadata.

When HALCON is unavailable the adapter reports ``available=False`` and
callers fall back to the documented NumPy path in roi.measurements.
"""

from __future__ import annotations

import time

import numpy as np

from thermal_monitor.halcon.geometry import halcon_params_for, verify_image_for_halcon
from thermal_monitor.roi.context import RoiActiveContext
from thermal_monitor.roi.errors import RoiStaleContextError, RoiValidationError
from thermal_monitor.roi.evaluator import RoiEvaluator
from thermal_monitor.roi.models import RoiDefinition, RoiMeasurement


def halcon_available() -> bool:
    try:
        import halcon  # noqa: F401
        return True
    except ImportError:
        return False


class HalconSnapshotRunner:
    """Executes ROI measurements for one immutable snapshot."""

    def __init__(self) -> None:
        self._fallback = RoiEvaluator()
        self._last_duration_ms = 0.0
        self._stale_discarded = 0

    @property
    def last_duration_ms(self) -> float:
        return self._last_duration_ms

    @property
    def stale_discarded(self) -> int:
        return self._stale_discarded

    def run(self, snapshot: np.ndarray, rois: list[RoiDefinition],
            context: RoiActiveContext, *, frame_id: int, frame_timestamp: float,
            publish_context: RoiActiveContext | None = None) -> list[RoiMeasurement]:
        image = np.asarray(snapshot).copy()
        image.setflags(write=False)
        current = publish_context if publish_context is not None else context
        if (context.context_generation != current.context_generation
                or context.position_id != current.position_id
                or context.camera_id != current.camera_id):
            self._stale_discarded += 1
            raise RoiStaleContextError("HALCON snapshot context superseded; discarded")
        try:
            verify_image_for_halcon(image, width=context.image_width,
                                    height=context.image_height)
        except RoiValidationError as exc:
            now = time.time()
            return [RoiMeasurement(
                roi_id=r.roi_id, camera_id=context.camera_id, ptz_id=context.ptz_id,
                position_id=context.position_id,
                context_generation=context.context_generation,
                session_generation=context.session_generation,
                frame_id=frame_id, frame_timestamp=frame_timestamp,
                processed_at=now, kind="no_data", values={"method": "verify_failed"},
                valid=False, error=str(exc)) for r in rois if r.enabled]
        # Validate geometry conversion up front (cheap, no HALCON needed).
        for roi in rois:
            halcon_params_for(roi.object_type, roi.geometry)
        t0 = time.perf_counter()
        try:
            if halcon_available():
                results = self._run_halcon(image, rois, context, frame_id, frame_timestamp)
            else:
                self._fallback.submit(np.asarray(image), frame_id, frame_timestamp)
                results = self._fallback.evaluate(
                    rois, context, session_generation=context.session_generation,
                    publish_context=current)
        finally:
            self._last_duration_ms = (time.perf_counter() - t0) * 1000.0
        if (context.context_generation != current.context_generation
                or context.position_id != current.position_id):
            self._stale_discarded += 1
            raise RoiStaleContextError("HALCON result superseded during execution; discarded")
        return results

    def _run_halcon(self, image, rois, context, frame_id, frame_timestamp):
        """HALCON path: reuse the proven adapter for area ROIs, NumPy for the rest."""
        import numpy as _np

        from thermal_monitor.core.models import ROIConfig, ROIGeometry, ROIShape
        from thermal_monitor.processing.halcon.roi_adapter import HalconROIAdapter

        area_rois = [r for r in rois if r.object_type.value in
                     ("rectangle", "ellipse", "circle", "polygon") and r.enabled]
        configs: list[ROIConfig] = []
        for r in rois:
            if r not in area_rois:
                continue
            g = r.geometry
            if r.object_type.value == "rectangle":
                shape, params = ROIShape.RECTANGLE1, {
                    "y1": g.row1, "x1": g.col1, "y2": g.row2, "x2": g.col2}
            elif r.object_type.value == "circle":
                shape, params = ROIShape.CIRCLE, {
                    "center_y": g.center_row, "center_x": g.center_col, "radius": g.radius}
            elif r.object_type.value == "ellipse":
                shape, params = ROIShape.ELLIPSE, {
                    "center_y": g.center_row, "center_x": g.center_col,
                    "phi": g.phi, "radius1": g.radius1, "radius2": g.radius2}
            else:
                shape, params = ROIShape.POLYGON, {
                    "points": [(p[0], p[1]) for p in g.points]}
            configs.append(ROIConfig(roi_id=r.roi_id, name=r.name,
                                     geometry=ROIGeometry(shape=shape, parameters=params)))
        halcon_stats: dict[str, object] = {}
        if configs:
            adapter = HalconROIAdapter()
            temp = _np.asarray(image, dtype=_np.float32)
            regions = adapter.generate_regions(configs)
            try:
                for stat, cfg in zip(
                        adapter.extract_statistics(regions, temp, rois=configs), configs):
                    halcon_stats[cfg.roi_id] = stat
            finally:
                adapter.release_regions(regions)
        # Merge: HALCON stats for area ROIs, documented NumPy for the rest.
        self._fallback.submit(_np.asarray(image), frame_id, frame_timestamp)
        numpy_results = {m.roi_id: m for m in self._fallback.evaluate(
            rois, context, session_generation=context.session_generation)}
        import time as _time
        out: list[RoiMeasurement] = []
        for r in rois:
            if r.roi_id in halcon_stats:
                s = halcon_stats[r.roi_id]
                out.append(RoiMeasurement(
                    roi_id=r.roi_id, camera_id=context.camera_id, ptz_id=context.ptz_id,
                    position_id=context.position_id,
                    context_generation=context.context_generation,
                    session_generation=context.session_generation,
                    frame_id=frame_id, frame_timestamp=frame_timestamp,
                    processed_at=_time.time(), kind="area_stats",
                    values={"min": s.min_temp, "max": s.max_temp, "mean": s.mean_temp,
                            "std": s.deviation, "method": "halcon_intensity_min_max_gray"},
                    valid=True))
            elif r.roi_id in numpy_results:
                out.append(numpy_results[r.roi_id])
        return out


__all__ = ["HalconSnapshotRunner", "halcon_available"]
