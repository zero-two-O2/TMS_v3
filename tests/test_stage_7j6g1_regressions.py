"""Regression tests for configuration-mode runtime, calibration, and FPS fixes."""

from pathlib import Path

import numpy as np
import pytest

from thermal_monitor.core.frame import Frame, FrameDescriptor, FramePayload, StreamMetadata, SyncInfo, SyncStatus
from thermal_monitor.core.models import AnalysisConfig
from thermal_monitor.processing.pipeline import SimpleProcessingPipeline
from thermal_monitor.processing.temperature import CachingCalibrationProvider, CPUTemperatureConverter
from thermal_monitor.ui.frame_rate import UniqueFrameRate


def _frame(camera_id: str = "cam_HB25080011") -> Frame:
    thermal = np.full((2, 2), 4000, dtype=np.uint16)
    thermal.setflags(write=False)
    descriptor = FrameDescriptor(
        camera_id=camera_id,
        sequence=1,
        timestamp=1.0,
        monotonic_timestamp=1.0,
        thermal=StreamMetadata(present=True, width=2, height=2, dtype="uint16"),
        visible=StreamMetadata(present=False),
        sync=SyncInfo(status=SyncStatus.MISSING_VISIBLE),
    )
    return Frame(descriptor=descriptor, payload=FramePayload(thermal=thermal))


def test_unique_frame_rate_ignores_repeated_sequence() -> None:
    rate = UniqueFrameRate()
    assert rate.add(101, now=10.0)
    assert not rate.add(101, now=10.1)
    assert rate.add(102, now=10.2)
    assert rate.count == 2
    assert rate.fps(now=11.0) == pytest.approx(2.0)


def test_tv46l_default_calibration_produces_physical_temperature() -> None:
    provider = CachingCalibrationProvider(app_root=Path("data"))
    lut = provider.get_calibration("cam_HB25080011")
    assert lut is not None
    temperature = CPUTemperatureConverter().raw_to_temperature(
        np.full((2, 2), 4000, dtype=np.uint16), lut, 0.95, 25.0, 1.0, 50.0, 20.0,
    )
    assert float(temperature[0, 0]) == pytest.approx(16.17, abs=0.1)
    assert float(temperature.max()) < 100.0


def test_missing_calibration_is_not_silently_treated_as_celsius() -> None:
    pipeline = SimpleProcessingPipeline(
        AnalysisConfig(camera_id="cam_missing"),
        calibration_provider=type("MissingProvider", (), {"get_calibration": lambda self, _: None})(),
        temperature_converter=CPUTemperatureConverter(),
    )
    with pytest.raises(RuntimeError, match="No valid calibration"):
        pipeline.process_frame(_frame("cam_missing"))
