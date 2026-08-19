#!/usr/bin/env python3
"""
V3 Temperature Conversion Benchmark Suite

Measures CPU and GPU temperature conversion performance using real TV46L frames.

Usage on NVIDIA PC:
    python scripts/benchmark_temperature_full.py

Outputs:
    - CPU baseline (LUT lookup, allocation, total conversion)
    - GPU conversion (host->GPU, GPU LUT, GPU->host, total)
    - CPU vs GPU correctness (max/mean abs diff, NaN handling, dtype, shape)
    - Multi-camera scaling estimates (1, 4, 8 cameras)
"""

from __future__ import annotations

import time
import statistics
import sys
from pathlib import Path
from typing import List, Tuple, Dict, Any

import numpy as np

from thermal_monitor.calibration.parser import CalibrationParser
from thermal_monitor.calibration.processor import CalibrationProcessor
from thermal_monitor.calibration.models import CameraCalibration
from thermal_monitor.processing.temperature import (
    CPUTemperatureConverter,
    GPUTemperatureConverter,
    CachingCalibrationProvider,
    is_gpu_available,
    get_gpu_device_name,
)
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
    
    for i, entry in enumerate(reader.entries):
        if i >= max_frames:
            break
        if entry.stream_type == 1:  # IR stream
            frame = reader.read_frame(entry)
            if frame.payload.thermal is not None:
                frames.append(frame.payload.thermal.copy())
    
    reader.close()
    return frames


def benchmark_cpu_conversion(
    converter: CPUTemperatureConverter,
    frames: List[np.ndarray],
    calibration: np.ndarray,
    warmup: int = 10,
) -> Dict[str, List[float]]:
    """Benchmark CPU raw_to_temperature conversion."""
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


def benchmark_gpu_conversion(
    converter: GPUTemperatureConverter,
    frames: List[np.ndarray],
    calibration: np.ndarray,
    warmup: int = 10,
) -> Dict[str, List[float]]:
    """Benchmark GPU raw_to_temperature conversion with detailed timing."""
    if not converter.is_ready():
        return {"error": "GPU converter not ready"}
    
    cp = converter._cp
    # Get the default LUT for benchmarking
    gpu_lut = converter._gpu_luts.get("__default__")
    if gpu_lut is None:
        return {"error": "Default LUT not loaded"}
    
    results = {
        "total_times_ms": [],
        "h2d_times_ms": [],
        "kernel_times_ms": [],
        "d2h_times_ms": [],
    }
    
    # Warmup (includes first LUT upload if not already done)
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
    
    # Benchmark with detailed timing
    for frame in frames:
        # Total time
        total_start = time.perf_counter()
        
        # Host to Device
        h2d_start = time.perf_counter()
        raw_uint16 = frame.astype(np.uint16, copy=False)
        gpu_raw = cp.asarray(raw_uint16)
        cp.cuda.Stream.null.synchronize()
        h2d_time = (time.perf_counter() - h2d_start) * 1000
        
        # GPU Kernel (LUT lookup)
        kernel_start = time.perf_counter()
        gpu_temp = gpu_lut[gpu_raw]
        cp.cuda.Stream.null.synchronize()
        kernel_time = (time.perf_counter() - kernel_start) * 1000
        
        # Device to Host
        d2h_start = time.perf_counter()
        temp_image = cp.asnumpy(gpu_temp)
        cp.cuda.Stream.null.synchronize()
        d2h_time = (time.perf_counter() - d2h_start) * 1000
        
        total_time = (time.perf_counter() - total_start) * 1000
        
        results["h2d_times_ms"].append(h2d_time)
        results["kernel_times_ms"].append(kernel_time)
        results["d2h_times_ms"].append(d2h_time)
        results["total_times_ms"].append(total_time)
    
    return results


def run_multi_camera_gpu_benchmark(
    converter: GPUTemperatureConverter,
    frames: List[np.ndarray],
    lut: np.ndarray,
    n_frames: int = 100,
) -> None:
    """Run concurrent multi-camera GPU benchmark.
    
    Loads LUTs for 1, 4, 8 cameras and processes frames concurrently
    to measure real throughput and verify LUT isolation.
    """
    cp = converter._cp
    test_frames = frames[:min(n_frames, len(frames))]
    
    for n_cams in [1, 4, 8]:
        print(f"\n--- {n_cams} Camera(s) Concurrent ---")
        
        # Create distinct LUTs for each camera
        luts = []
        for i in range(n_cams):
            # Slightly different scaling to verify isolation
            scale = 0.01 + i * 0.005  # 0.01, 0.015, 0.02, ...
            camera_lut = np.arange(65536, dtype=np.float32) * scale
            camera_id = f"bench_cam_{i}"
            converter.load_lut(camera_id, camera_lut)
            luts.append((camera_id, camera_lut, scale))
        
        # Warmup
        for frame in test_frames[:10]:
            for camera_id, camera_lut, _ in luts:
                _ = converter.raw_to_temperature(
                    raw_data=frame,
                    calibration=camera_lut,
                    emissivity=1.0, ambient_temp=25.0, distance=1.0,
                    humidity=50.0, reflected_temp=25.0,
                    camera_id=camera_id
                )
        cp.cuda.Stream.null.synchronize()
        
        # Benchmark concurrent processing
        # Process frames round-robin across cameras
        total_start = time.perf_counter()
        frame_count = 0
        
        for frame in test_frames:
            for camera_id, camera_lut, scale in luts:
                # Time the full conversion
                start = time.perf_counter()
                result = converter.raw_to_temperature(
                    raw_data=frame,
                    calibration=camera_lut,
                    emissivity=1.0, ambient_temp=25.0, distance=1.0,
                    humidity=50.0, reflected_temp=25.0,
                    camera_id=camera_id
                )
                cp.cuda.Stream.null.synchronize()
                elapsed = (time.perf_counter() - start) * 1000
                
                # Verify correctness: check that the correct LUT was used
                expected_temp = camera_lut[frame.flat[0]]  # First pixel
                actual_temp = result.flat[0]
                if not np.isnan(expected_temp) and not np.isnan(actual_temp):
                    diff = abs(actual_temp - expected_temp)
                    if diff > 1e-4:
                        print(f"  WARNING: Camera {camera_id} LUT mismatch! "
                              f"Expected {expected_temp:.3f}, got {actual_temp:.3f}")
                
                frame_count += 1
        
        total_elapsed = (time.perf_counter() - total_start) * 1000
        mean_per_conversion = total_elapsed / frame_count
        aggregate_fps = 1000.0 / mean_per_conversion if mean_per_conversion > 0 else 0
        per_camera_fps = aggregate_fps / n_cams
        
        print(f"  Total conversions: {frame_count}")
        print(f"  Total time: {total_elapsed:.2f} ms")
        print(f"  Mean per conversion: {mean_per_conversion:.3f} ms")
        print(f"  Aggregate throughput: {aggregate_fps:.1f} FPS")
        print(f"  Per-camera throughput: {per_camera_fps:.1f} FPS")
        
        # Verify each camera's LUT is isolated
        print(f"  Loaded cameras: {converter.get_loaded_cameras()}")
        
        # Release test LUTs
        for camera_id, _, _ in luts:
            converter.release(camera_id)
        
        # Reload default LUT for subsequent tests
        converter.load_lut("__default__", lut)


def calculate_stats(times_ms: List[float]) -> Dict[str, float]:
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


def compare_cpu_gpu_output(
    cpu_result: np.ndarray,
    gpu_result: np.ndarray,
) -> Dict[str, Any]:
    """Compare CPU and GPU outputs for numerical correctness."""
    results = {}
    
    # Shape and dtype
    results["cpu_shape"] = cpu_result.shape
    results["gpu_shape"] = gpu_result.shape
    results["shape_match"] = cpu_result.shape == gpu_result.shape
    results["cpu_dtype"] = str(cpu_result.dtype)
    results["gpu_dtype"] = str(gpu_result.dtype)
    results["dtype_match"] = cpu_result.dtype == gpu_result.dtype
    
    # NaN handling
    cpu_nan = np.isnan(cpu_result)
    gpu_nan = np.isnan(gpu_result)
    results["cpu_nan_count"] = int(np.sum(cpu_nan))
    results["gpu_nan_count"] = int(np.sum(gpu_nan))
    results["nan_positions_match"] = np.array_equal(cpu_nan, gpu_nan)
    
    # Finite value comparison
    both_finite = np.isfinite(cpu_result) & np.isfinite(gpu_result)
    if np.any(both_finite):
        cpu_finite = cpu_result[both_finite]
        gpu_finite = gpu_result[both_finite]
        diff = np.abs(cpu_finite - gpu_finite)
        results["max_abs_diff"] = float(np.max(diff))
        results["mean_abs_diff"] = float(np.mean(diff))
        results["median_abs_diff"] = float(np.median(diff))
        results["p95_abs_diff"] = float(np.percentile(diff, 95))
        results["max_rel_diff"] = float(np.max(diff / np.maximum(np.abs(cpu_finite), 1e-10)))
    else:
        results["max_abs_diff"] = float('nan')
        results["mean_abs_diff"] = float('nan')
        results["median_abs_diff"] = float('nan')
        results["p95_abs_diff"] = float('nan')
        results["max_rel_diff"] = float('nan')
    
    # Overall
    results["numerically_equivalent"] = (
        results["shape_match"]
        and results["dtype_match"]
        and results["nan_positions_match"]
        and results["max_abs_diff"] < 1e-5
    )
    
    return results


def print_stats(label: str, stats: Dict[str, float]) -> None:
    """Print formatted statistics."""
    if "error" in stats:
        print(f"\n{label}: {stats['error']}")
        return
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


def print_comparison(label: str, cmp: Dict[str, Any]) -> None:
    """Print CPU vs GPU comparison results."""
    print(f"\n{label}")
    print(f"  Shape:          CPU {cmp['cpu_shape']}  GPU {cmp['gpu_shape']}  {'✓' if cmp['shape_match'] else '✗'}")
    print(f"  Dtype:          CPU {cmp['cpu_dtype']}  GPU {cmp['gpu_dtype']}  {'✓' if cmp['dtype_match'] else '✗'}")
    print(f"  NaN count:      CPU {cmp['cpu_nan_count']}  GPU {cmp['gpu_nan_count']}  {'✓' if cmp['nan_positions_match'] else '✗'}")
    print(f"  Max abs diff:   {cmp['max_abs_diff']:.6f}")
    print(f"  Mean abs diff:  {cmp['mean_abs_diff']:.6f}")
    print(f"  Median abs diff:{cmp['median_abs_diff']:.6f}")
    print(f"  P95 abs diff:   {cmp['p95_abs_diff']:.6f}")
    print(f"  Max rel diff:   {cmp['max_rel_diff']:.6f}")
    print(f"  Equivalent:     {'✓ YES' if cmp['numerically_equivalent'] else '✗ NO'}")


def main():
    print("=" * 70)
    print("V3 Temperature Conversion Benchmark Suite")
    print("=" * 70)
    
    # System info
    print(f"\nPython: {sys.version.split()[0]}")
    print(f"NumPy:  {np.__version__}")
    
    gpu_avail = is_gpu_available()
    gpu_name = get_gpu_device_name()
    print(f"GPU Available: {gpu_avail}")
    if gpu_name:
        print(f"GPU Device:    {gpu_name}")
    
    # Load calibration
    print("\n" + "=" * 70)
    print("Loading calibration...")
    print("=" * 70)
    calibration = load_calibration()
    lut = calibration.get_lookup_table(0)
    print(f"LUT shape: {lut.shape}, dtype: {lut.dtype}")
    print(f"LUT range: [{np.nanmin(lut):.2f}, {np.nanmax(lut):.2f}] °C")
    
    # Load real frames
    print("\nLoading real frames from hardware_validation recording...")
    recording_dir = Path("recordings/hardware_validation")
    frames = load_real_frames(recording_dir, max_frames=500)
    
    if not frames:
        print("ERROR: No frames loaded!")
        return
    
    print(f"Loaded {len(frames)} frames, shape: {frames[0].shape}, dtype: {frames[0].dtype}")
    print(f"Frame value range: [{frames[0].min()}, {frames[0].max()}]")
    
    # Create converters
    cpu_converter = CPUTemperatureConverter()
    
    gpu_converter = None
    if gpu_avail:
        gpu_converter = GPUTemperatureConverter()
        # Load default LUT for GPU benchmarks
        gpu_converter.load_lut("__default__", lut)
        print(f"\nGPU Converter initialized: {gpu_converter.is_ready()}")
        if gpu_converter.is_ready():
            print(f"GPU Device: {gpu_converter.get_device_name()}")
    
    # ============================================================
    # CPU BENCHMARKS
    # ============================================================
    print("\n" + "=" * 70)
    print("CPU BENCHMARKS")
    print("=" * 70)
    
    for n_frames, label in [(100, "100 frames"), (len(frames), f"{len(frames)} frames")]:
        print(f"\n--- {label} ---")
        cpu_results = benchmark_cpu_conversion(cpu_converter, frames[:n_frames], lut)
        
        print_stats(f"TOTAL CONVERSION (CPU)", calculate_stats(cpu_results["total_times_ms"]))
        print_stats(f"LUT LOOKUP ONLY", calculate_stats(cpu_results["lookup_times_ms"]))
        print_stats(f"ALLOC/COPY ONLY", calculate_stats(cpu_results["alloc_times_ms"]))
    
    # ============================================================
    # GPU BENCHMARKS (if available)
    # ============================================================
    if gpu_converter and gpu_converter.is_ready():
        print("\n" + "=" * 70)
        print("GPU BENCHMARKS")
        print("=" * 70)
        
        for n_frames, label in [(100, "100 frames"), (len(frames), f"{len(frames)} frames")]:
            print(f"\n--- {label} ---")
            gpu_results = benchmark_gpu_conversion(gpu_converter, frames[:n_frames], lut)
            
            if "error" not in gpu_results:
                print_stats(f"TOTAL GPU CONVERSION", calculate_stats(gpu_results["total_times_ms"]))
                print_stats(f"  Host -> GPU (H2D)", calculate_stats(gpu_results["h2d_times_ms"]))
                print_stats(f"  GPU Kernel (LUT)", calculate_stats(gpu_results["kernel_times_ms"]))
                print_stats(f"  GPU -> Host (D2H)", calculate_stats(gpu_results["d2h_times_ms"]))
                
                # Breakdown percentages
                total_mean = calculate_stats(gpu_results["total_times_ms"])["mean_ms"]
                h2d_mean = calculate_stats(gpu_results["h2d_times_ms"])["mean_ms"]
                kernel_mean = calculate_stats(gpu_results["kernel_times_ms"])["mean_ms"]
                d2h_mean = calculate_stats(gpu_results["d2h_times_ms"])["mean_ms"]
                
                print(f"\n  Breakdown (% of total):")
                print(f"    H2D:   {h2d_mean/total_mean*100:.1f}%")
                print(f"    Kernel: {kernel_mean/total_mean*100:.1f}%")
                print(f"    D2H:   {d2h_mean/total_mean*100:.1f}%")
            else:
                print(f"GPU Benchmark failed: {gpu_results['error']}")
        
        # Multi-camera concurrent GPU benchmark
        print("\n" + "=" * 70)
        print("MULTI-CAMERA CONCURRENT GPU BENCHMARK")
        print("=" * 70)
        run_multi_camera_gpu_benchmark(gpu_converter, frames, lut)
    else:
        print("\n" + "=" * 70)
        print("GPU BENCHMARKS: SKIPPED (GPU not available on this machine)")
        print("=" * 70)
        print("Run this script on the NVIDIA PC to measure GPU performance.")
    
    # ============================================================
    # CORRECTNESS COMPARISON (CPU vs GPU)
    # ============================================================
    if gpu_converter and gpu_converter.is_ready():
        print("\n" + "=" * 70)
        print("CPU vs GPU CORRECTNESS")
        print("=" * 70)
        
        # Test on first 10 frames
        for i in range(min(10, len(frames))):
            cpu_out = cpu_converter.raw_to_temperature(
                frames[i], lut, 1.0, 25.0, 1.0, 50.0, 25.0
            )
            gpu_out = gpu_converter.raw_to_temperature(
                frames[i], lut, 1.0, 25.0, 1.0, 50.0, 25.0
            )
            cmp = compare_cpu_gpu_output(cpu_out, gpu_out)
            if i == 0:
                print_comparison(f"Frame {i} (first frame)", cmp)
            elif not cmp["numerically_equivalent"]:
                print_comparison(f"Frame {i} (MISMATCH)", cmp)
        
        # Summary
        all_equivalent = True
        max_diff = 0.0
        for i in range(min(10, len(frames))):
            cpu_out = cpu_converter.raw_to_temperature(frames[i], lut, 1.0, 25.0, 1.0, 50.0, 25.0)
            gpu_out = gpu_converter.raw_to_temperature(frames[i], lut, 1.0, 25.0, 1.0, 50.0, 25.0)
            cmp = compare_cpu_gpu_output(cpu_out, gpu_out)
            if not cmp["numerically_equivalent"]:
                all_equivalent = False
            max_diff = max(max_diff, cmp["max_abs_diff"])
        
        print(f"\n  Overall: {'✓ ALL FRAMES EQUIVALENT' if all_equivalent else '✗ MISMATCHES FOUND'}")
        print(f"  Max abs diff across frames: {max_diff:.6f}")
    else:
        print("\n" + "=" * 70)
        print("CPU vs GPU CORRECTNESS: SKIPPED (GPU not available)")
        print("=" * 70)
    
    # ============================================================
    # MULTI-CAMERA SCALING ESTIMATES
    # ============================================================
    print("\n" + "=" * 70)
    print("MULTI-CAMERA SCALING ESTIMATES (software workload simulation)")
    print("=" * 70)
    print("Based on CPU mean per-frame time. Not hardware acquisition validation.")
    
    cpu_stats = calculate_stats(benchmark_cpu_conversion(cpu_converter, frames[:200], lut)["total_times_ms"])
    cpu_mean = cpu_stats["mean_ms"]
    
    print(f"\n  Single camera: {cpu_mean:.3f} ms/frame  ({cpu_stats['fps']:.1f} FPS)")
    for n_cams in [1, 4, 8]:
        aggregate_fps = 1000.0 / (cpu_mean * n_cams) if cpu_mean > 0 else 0
        total_ms = cpu_mean * n_cams
        print(f"  {n_cams} cameras:   {total_ms:.2f} ms total per frame set  "
              f"({aggregate_fps:.1f} aggregate FPS)")
    
    if gpu_converter and gpu_converter.is_ready():
        gpu_stats = calculate_stats(benchmark_gpu_conversion(gpu_converter, frames[:200], lut)["total_times_ms"])
        gpu_mean = gpu_stats["mean_ms"]
        print(f"\n  GPU single camera: {gpu_mean:.3f} ms/frame  ({gpu_stats['fps']:.1f} FPS)")
        for n_cams in [1, 4, 8]:
            aggregate_fps = 1000.0 / (gpu_mean * n_cams) if gpu_mean > 0 else 0
            total_ms = gpu_mean * n_cams
            speedup = cpu_mean / gpu_mean if gpu_mean > 0 else 0
            print(f"  GPU {n_cams} cameras:  {total_ms:.2f} ms total per frame set  "
                  f"({aggregate_fps:.1f} aggregate FPS, {speedup:.2f}x speedup)")
    
    # ============================================================
    # MEMORY ESTIMATES
    # ============================================================
    print("\n" + "=" * 70)
    print("MEMORY ESTIMATES")
    print("=" * 70)
    frame_bytes = frames[0].nbytes
    lut_bytes = lut.nbytes
    print(f"  Frame (uint16 640x480): {frame_bytes / 1024:.1f} KB")
    print(f"  LUT (float32 65536):    {lut_bytes / 1024:.1f} KB")
    print(f"  Temp output (float32):  {frame_bytes * 2 / 1024:.1f} KB")
    for n_cams in [1, 4, 8]:
        gpu_mem = lut_bytes + n_cams * (frame_bytes + frame_bytes * 2)
        print(f"  GPU mem ({n_cams} cam):   ~{gpu_mem / 1024 / 1024:.2f} MB "
              f"(LUT + {n_cams}×(input + output))")
    
    print("\n" + "=" * 70)
    print("BENCHMARK COMPLETE")
    print("=" * 70)


if __name__ == "__main__":
    main()