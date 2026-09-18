"""Phase 12.2 -- isolated HALCON behavior proofs (no production code touched).

Each test verifies one claim the Phase 12.3 implementation will rely on,
directly against the installed HALCON Python binding:

- batched intensity/min_max_gray/area_center tuple alignment + ordering
- mixed-shape concat_obj ordering
- empty-object safety (no exception, empty tuples)
- himage_from_numpy_array deep-copy lifetime (mutate-after-convert)
- non-contiguous input handling
- NaN propagation semantics
- degenerate rectangle rejection (must validate before generation)
- Rectangle1 parity: batched HALCON == per-ROI HALCON == NumPy window
  (integer coords, tolerance 1e-6)
- per-ROI loop vs batched call-count reduction (3n -> 3 per shape group)

Skipped when HALCON is not installed (hardware-blocked, not a failure).
"""

import numpy as np
import pytest

ha = pytest.importorskip("halcon", reason="HALCON binding not installed")


@pytest.fixture(autouse=True)
def _no_clip():
    ha.set_system("clip_region", "false")
    yield


def _himage(arr: np.ndarray):
    return ha.himage_from_numpy_array(np.ascontiguousarray(arr, dtype=np.float32))


def _gradient(h=480, w=640):
    rr = np.arange(h, dtype=np.float32).reshape(-1, 1)
    cc = np.arange(w, dtype=np.float32).reshape(1, -1)
    return (rr * 0.1 + cc * 0.01).astype(np.float32)


def test_batched_stats_align_with_input_order():
    img = np.zeros((480, 640), dtype=np.float32)
    img[100:200, 100:200] = 50.0
    img[300:400, 300:400] = 100.0
    himg = _himage(img)
    regs = ha.gen_rectangle1([100, 300], [100, 300], [199, 399], [199, 399])
    assert ha.count_obj(regs) == 2
    mean, dev = ha.intensity(regs, himg)
    mn, mx, rg = ha.min_max_gray(regs, himg, 0)
    area, crow, ccol = ha.area_center(regs)
    assert list(mean) == pytest.approx([50.0, 100.0], abs=1e-6)
    assert list(dev) == pytest.approx([0.0, 0.0], abs=1e-6)
    assert list(mn) == pytest.approx([50.0, 100.0], abs=1e-6)
    assert list(mx) == pytest.approx([50.0, 100.0], abs=1e-6)
    assert list(rg) == pytest.approx([0.0, 0.0], abs=1e-6)
    assert list(area) == pytest.approx([10000, 10000], abs=1e-6)
    assert list(crow) == pytest.approx([149.5, 349.5], abs=1e-6)
    assert list(ccol) == pytest.approx([149.5, 349.5], abs=1e-6)


def test_batched_matches_per_roi_loop():
    img = _gradient()
    himg = _himage(img)
    boxes = [(10, 10, 60, 60), (100, 200, 180, 320), (400, 500, 470, 630)]
    regs = ha.gen_rectangle1(
        [b[0] for b in boxes], [b[1] for b in boxes],
        [b[2] for b in boxes], [b[3] for b in boxes],
    )
    bmean, bdev = ha.intensity(regs, himg)
    bmn, bmx, _ = ha.min_max_gray(regs, himg, 0)
    for i, (r1, c1, r2, c2) in enumerate(boxes, start=1):
        single = ha.select_obj(regs, i)
        smean, sdev = ha.intensity(single, himg)
        smn, smx, _ = ha.min_max_gray(single, himg, 0)
        assert float(smean[0]) == pytest.approx(float(bmean[i - 1]), abs=1e-6)
        assert float(sdev[0]) == pytest.approx(float(bdev[i - 1]), abs=1e-6)
        assert float(smn[0]) == pytest.approx(float(bmn[i - 1]), abs=1e-6)
        assert float(smx[0]) == pytest.approx(float(bmx[i - 1]), abs=1e-6)


def test_mixed_shape_concat_preserves_order():
    img = np.zeros((480, 640), dtype=np.float32)
    img[50:150, 50:150] = 60.0
    img[200:260, 400:460] = 80.0
    himg = _himage(img)
    r1 = ha.gen_rectangle1([50], [50], [149], [149])
    c1 = ha.gen_circle([230.0], [430.0], [30.0])
    mixed = ha.concat_obj(r1, c1)
    assert ha.count_obj(mixed) == 2
    mean, _ = ha.intensity(mixed, himg)
    assert list(mean) == pytest.approx([60.0, 80.0], abs=1e-4)


def test_empty_object_returns_empty_tuples():
    himg = _himage(np.full((64, 64), 25.0, dtype=np.float32))
    empty = ha.gen_empty_obj()
    assert ha.count_obj(empty) == 0
    assert ha.intensity(empty, himg) == ([], [])
    assert ha.min_max_gray(empty, himg, 0) == ([], [], [])
    assert ha.area_center(empty) == ([], [], [])


def test_image_conversion_deep_copies():
    img = np.full((100, 100), 40.0, dtype=np.float32)
    imgc = np.ascontiguousarray(img)
    himg = ha.himage_from_numpy_array(imgc)
    imgc[:, :] = 999.0  # mutate backing store after conversion
    regs = ha.gen_rectangle1([10], [10], [20], [20])
    mean, _ = ha.intensity(regs, himg)
    assert list(mean) == pytest.approx([40.0], abs=1e-6)


def test_non_contiguous_input_accepted():
    nc = np.full((100, 100), 55.0, dtype=np.float32)[::2, ::2]
    assert not nc.flags["C_CONTIGUOUS"]
    himg = ha.himage_from_numpy_array(nc)
    regs = ha.gen_rectangle1([5], [5], [10], [10])
    mean, _ = ha.intensity(regs, himg)
    assert list(mean) == pytest.approx([55.0], abs=1e-6)


def test_nan_region_propagates_nan():
    img = np.full((50, 50), 30.0, dtype=np.float32)
    img[10:20, 10:20] = np.nan
    himg = _himage(img)
    regs = ha.gen_rectangle1([10], [10], [19], [19])
    mean, _ = ha.intensity(regs, himg)
    mn, mx, _ = ha.min_max_gray(regs, himg, 0)
    assert np.isnan(float(mean[0]))
    assert np.isnan(float(mn[0])) and np.isnan(float(mx[0]))
    # Callers must map NaN results to valid=False, never to 0.0.


def test_degenerate_rectangle_rejected_by_halcon():
    with pytest.raises(Exception):
        ha.gen_rectangle1([10], [10], [5], [5])  # row2 < row1
    with pytest.raises(Exception):
        ha.gen_rectangle1([10], [10], [10], [5])  # col2 < col1
    # Consequence: validate row2 > row1 and col2 > col1 BEFORE generation.


def test_rectangle1_parity_with_numpy_window():
    img = _gradient()
    himg = _himage(img)
    for r1, c1, r2, c2 in [(100, 100, 199, 199), (0, 0, 9, 9), (5, 5, 5, 5)]:
        regs = ha.gen_rectangle1([r1], [c1], [r2], [c2])
        mean, _ = ha.intensity(regs, himg)
        expected = float(np.mean(img[r1:r2 + 1, c1:c2 + 1]))
        # float32 accumulation on both sides: relative tolerance, not 1e-6 abs.
        assert float(mean[0]) == pytest.approx(expected, rel=1e-5)


def test_out_of_bounds_rectangle_is_clipped():
    img = _gradient()
    himg = _himage(img)
    regs = ha.gen_rectangle1([470], [630], [500], [700])
    mean, _ = ha.intensity(regs, himg)
    expected = float(np.mean(img[470:480, 630:640]))
    assert float(mean[0]) == pytest.approx(expected, abs=1e-3)


def test_polygon_single_region_and_concat():
    himg = _himage(np.zeros((480, 640), dtype=np.float32))
    p = ha.gen_region_polygon_filled([300.0, 300.0, 350.0], [100.0, 150.0, 125.0])
    assert ha.count_obj(p) == 1
    r1 = ha.gen_rectangle1([50], [50], [60], [60])
    combined = ha.concat_obj(r1, p)
    assert ha.count_obj(combined) == 2
    mean, _ = ha.intensity(combined, himg)
    assert len(mean) == 2
