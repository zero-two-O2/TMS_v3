#!/usr/bin/env python3
"""
CPU Temperature Conversion Benchmark

Benchmarks the V3 CPUTemperatureConverter using real TV46L frames from
recordings/hardware_validation.

Measures:
- Total time, mean, median, p95, min, max, throughput FPS
- Separate: LUT lookup, allocation/copy, complete conversion
"""

from __future__ import annotations

import time
import statistics
from pathlib import Path
from typing import List, Tuple

import numpy as np

from thermal_monitor.calibration.parser import CalibrationParser
from thermal_monitor.calibration.processor import CalibrationProcessor
from thermal_monitor.calibration.models import CameraCalibration
from thermal_monitor.processing.temperature import CPUTemperatureConverter, CachingCalibrationProvider
from thermal_monitor.offline.reader import RecordingReader


def load_calibration() -> CameraCalibration:
    """Load and build calibration LUT from default location."""
    parser = CalibrationParser()
    calibration_path = Path("assets/calibration/calibration_blob.txt")
    if not calibration_path.exists():
        raise FileNotFoundError(f"Calibration file not found: {calibration_path}")
    calibration = parser.load(calibration_path)
    CalibrationProcessor.build_lookup_tables(calibration)
    return calibration


def load_real_frames(recording_dir: Path, max_frames: int = 500) -> List[np.ndarray]:
    """Load raw thermal frames from recording."""
    reader = RecordingReader(recording_dir)
    frames = []
    
    print(f"Recording status: {reader.status}")
    print(f"Frame count: {reader.frame_count}")
    print(f"Cameras: {reader.camera_ids}")
    
    for i, entry in enumerate(reader.entries):
        if i >= max_frames:
            break
        if entry.stream_type == 1:  # IR stream
            frame = reader.read_frame(entry)
            if frame.payload.thermal is not None:
                # Make a copy to avoid any shared memory issues
                frames.append(frame.payload.thermal.copy())
    
    reader.close()
    print(f"Loaded {len(frames)} thermal frames")
    return frames


def benchmark_conversion(
    converter: CPUTemperatureConverter,
    frames: List[np.ndarray],
    calibration: np.ndarray,
    warmup: int = 10,
) -> dict:
    """Benchmark the raw_to_temperature conversion."""
    results = {
        "total_times_ms": [],
        "lookup_times_ms": [],
        "alloc_times_ms": [],
    }
    
    # Warmup
    for i in range(min(warmup, len(frames))):
        _ = converter.raw_to_temperature(
            raw_data=frames[i],
            calibration=calibration,
            emissivity=1.0,
            ambient_temp=25.0,
            distance=1.0,
            humidity=50.0,
            reflected_temp=25.0,
        )
    
    # Benchmark
    for frame in frames:
        # Measure allocation/copy time
        alloc_start = time.perf_counter()
        raw_uint16 = frame.astype(np.uint16, copy=False)
        alloc_time = (time.perf_counter() - alloc_start) * 1000
        
        # Measure LUT lookup time
        lookup_start = time.perf_counter()
        temp_image = calibration[raw_uint16]
        lookup_time = (time.perf_counter() - lookup_start) * 1000
        
        # Total conversion time (using converter method)
        total_start = time.perf_counter()
        _ = converter.raw_to_temperature(
            raw_data=frame,
            calibration=calibration,
            emissivity=1.0,
            ambient_temp=25.0,
            distance=1.0,
            humidity=50.0,
            reflected_temp=25.0,
        )
        total_time = (time.perf_counter() - total_start) * 1000
        
        results["alloc_times_ms"].append(alloc_time)
        results["lookup_times_ms"].append(lookup_time)
        results["total_times_ms"].append(total_time)
    
    return results


def calculate_stats(times_ms: List[float]) -> dict:
    """Calculate statistics from timing measurements."""
    if not times_ms:
        return {}
    sorted_times = sorted(times_ms)
    n = len(sorted_times)
    return {
        "count": n,
        "total_ms": sum(sorted_times),
        "mean_ms": statistics.mean(sorted_times),
        "median_ms": statistics.median(sorted_times),
        "stdev_ms": statistics.stdev(sorted_times) if n > 1 else 0.0,
        "min_ms": min(sorted_times),
        "max_ms": max(sorted_times),
        "p95_ms": sorted_times[int(n * 0.95)],
        "p99_ms": sorted_times[int(n * 0.99)] if n > 100 else sorted_times[-1],
        "fps": 1000.0 / statistics.mean(sorted_times) if statistics.mean(sorted_times) > 0 else 0.0,
    }


def print_stats(label: str, stats: dict) -> None:
    """Print formatted statistics."""
    print(f"\n{label}")
    print(f"  Count:     {stats['count']}")
    print(f"  Total:     {stats['total_ms']:.2f} ms")
    print(f"  Mean:      {stats['mean_ms']:.3f} ms")
    print(f"  Median:    {stats['median_ms']:.3f} ms")
    print(f"  StDev:     {stats['stdev_ms']:.3f} ms")
    print(f"  Min:       {stats['min_ms']:.3f} ms")
    print(f"  Max:       {stats['max_ms']:.3f} ms")
    print(f"  P95:       {stats['p95_ms']:.3f} ms")
    print(f"  P99:       {stats['p99_ms']:.3f} ms")
    print(f"  Throughput: {stats['fps']:.1f} FPS")


def main():
    print("=" * 60)
    print("V3 CPU Temperature Conversion Benchmark")
    print("=" * 60)
    
    # Load calibration
    print("\nLoading calibration...")
    calibration = load_calibration()
    lut = calibration.get_lookup_table(0)
    print(f"LUT shape: {lut.shape}, dtype: {lut.dtype}")
    
    # Load real frames
    print("\nLoading real frames from hardware_validation recording...")
    recording_dir = Path("recordings/hardware_validation")
    frames = load_real_frames(recording_dir, max_frames=500)
    
    if not frames:
        print("ERROR: No frames loaded!")
        return
    
    print(f"Frame shape: {frames[0].shape}, dtype: {frames[0].dtype}")
    
    # Create converter
    converter = CPUTemperatureConverter()
    
    # Run benchmarks
    print("\n" + "=" * 60)
    print("BENCHMARK: 100 frames")
    print("=" * 60)
    results_100 = benchmark_conversion(converter, frames[:100], lut, warmup=10)
    
    print_stats("TOTAL CONVERSION (converter.raw_to_temperature)", 
                calculate_stats(results_100["total_times_ms"]))
    print_stats("LUT LOOKUP ONLY (calibration[raw_uint16])", 
                calculate_stats(results_100["lookup_times_ms"]))
    print_stats("ALLOC/COPY ONLY (frame.astype)", 
                calculate_stats(results_100["alloc_times_ms"]))
    
    print("\n" + "=" * 60)
    print("BENCHMARK: 500 frames")
    print("=" * 60)
    results_500 = benchmark_conversion(converter, frames[:500], lut, warmup=10)
    
    print_stats("TOTAL CONVERSION (converter.raw_to_temperature)", 
                calculate_stats(results_500["total_times_ms"]))
    print_stats("LUT LOOKUP ONLY (calibration[raw_uint16])", 
                calculate_stats(results_500["lookup_times_ms"]))
    print_stats("ALLOC/COPY ONLY (frame.astype)", 
                calculate_stats(results_500["alloc_times_ms"]))
    
    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    total_100 = calculate_stats(results_100["total_times_ms"])
    total_500 = calculate_stats(results_500["total_times_ms"])
    lookup_100 = calculate_stats(results_100["lookup_times_ms"])
    lookup_500 = calculate_stats(results_500["lookup_times_ms"])
    
    print(f"100 frames - Total:  {total_100['mean_ms']:.3f} ms/frame ({total_100['fps']:.1f} FPS)")
    print(f"500 frames - Total:  {total_500['mean_ms']:.3f} ms/frame ({total_500['fps']:.1f} FPS)")
    print(f"100 frames - Lookup: {lookup_100['mean_ms']:.3f} ms/frame ({lookup_100['fps']:.1f} FPS)")
    print(f"500 frames - Lookup: {lookup_500['mean_ms']:.3f} ms/frame ({lookup_500['fps']:.1f} FPS)")
    
    # Estimate multi-camera scaling
    print("\n" + "=" * 60)
    print("MULTI-CAMERA SCALING ESTIMATE (software workload simulation)")
    print("=" * 60)
    mean_per_frame = total_500['mean_ms']
    for n_cams in [1, 4, 8]:
        aggregate_fps = 1000.0 / (mean_per_frame * n_cams) if mean_per_frame > 0 else 0
        print(f"  {n_cams} cameras: ~{aggregate_fps:.1f} aggregate FPS "
              f"({mean_per_frame * n_cams:.2f} ms total per frame set)")
    
    return {
        "frames_100": results_100,
        "frames_500": results_500,
        "calibration_lut_shape": lut.shape,
    }


if __name__ == "__main__":
    main()