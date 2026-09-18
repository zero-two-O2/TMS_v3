"""Phase 12.3 A+B: grouped ROI views and worker-owned RegionCache.

Group A (no HALCON needed): shape grouping, deterministic ordering,
parallel tuple alignment, invalid geometry rejection, empty groups.
Group B (HALCON): initial build, cache hit, per-shape rebuild, camera /
position / generation invalidation, clear/release, worker isolation.
"""

import pytest

from thermal_monitor.core.models import ROIConfig, ROIGeometry, ROIShape
from thermal_monitor.processing.halcon.grouped import (
    SHAPE_ORDER,
    build_shape_groups,
)
from thermal_monitor.processing.halcon.region_cache import RegionCache

ha = pytest.importorskip("halcon", reason="HALCON binding not installed")


def _rect(rid, y1, x1, y2, x2, name="r", enabled=True):
    return ROIConfig(
        roi_id=rid, name=name,
        geometry=ROIGeometry(
            shape=ROIShape.RECTANGLE1,
            parameters={"y1": y1, "x1": x1, "y2": y2, "x2": x2}),
        enabled=enabled,
    )


def _circle(rid, cy, cx, radius, name="c"):
    return ROIConfig(
        roi_id=rid, name=name,
        geometry=ROIGeometry(
            shape=ROIShape.CIRCLE,
            parameters={"center_y": cy, "center_x": cx, "radius": radius}),
    )


CTX = {"camera_id": "cam1", "position_id": "p1", "context_generation": 3}


def _build_adapter():
    from thermal_monitor.processing.halcon import HalconROIAdapter
    ad = HalconROIAdapter()
    ad._configure_clip_region(ha)
    return ad


# ---------------- Group A: grouped views ----------------

def test_rectangle1_grouping_and_alignment():
    rois = [_rect("b", 10, 10, 20, 20), _rect("a", 30, 30, 40, 40)]
    groups, invalid = build_shape_groups(rois, **CTX)
    assert invalid == ()
    assert len(groups) == 1
    g = groups[0]
    assert g.shape == ROIShape.RECTANGLE1
    assert g.roi_ids == ("b", "a")  # input order preserved
    assert g.roi_names == ("r", "r")
    assert g.params == ((10, 10, 20, 20), (30, 30, 40, 40))
    assert len(g.params) == len(g.roi_ids)
    assert g.camera_id == "cam1" and g.context_generation == 3


def test_shape_group_order_is_deterministic():
    rois = [
        _circle("c1", 50.0, 50.0, 10.0),
        _rect("r1", 0, 0, 9, 9),
    ]
    groups, _ = build_shape_groups(rois, **CTX)
    assert [g.shape for g in groups] == [ROIShape.RECTANGLE1, ROIShape.CIRCLE]
    assert [s for s in SHAPE_ORDER].index(ROIShape.RECTANGLE1) < \
        [s for s in SHAPE_ORDER].index(ROIShape.CIRCLE)


def test_all_five_shapes_grouped():
    rois = [
        _rect("r1", 0, 0, 9, 9),
        ROIConfig(roi_id="r2", name="r2", geometry=ROIGeometry(
            shape=ROIShape.RECTANGLE2, parameters={
                "center_y": 50.0, "center_x": 50.0, "phi": 0.0,
                "length1": 20.0, "length2": 10.0})),
        _circle("c1", 60.0, 60.0, 8.0),
        ROIConfig(roi_id="e1", name="e1", geometry=ROIGeometry(
            shape=ROIShape.ELLIPSE, parameters={
                "center_y": 70.0, "center_x": 70.0, "phi": 0.0,
                "radius1": 12.0, "radius2": 6.0})),
        ROIConfig(roi_id="p1", name="p1", geometry=ROIGeometry(
            shape=ROIShape.POLYGON, parameters={
                "points": [(100.0, 100.0), (100.0, 120.0), (120.0, 110.0)]})),
    ]
    groups, invalid = build_shape_groups(rois, **CTX)
    assert invalid == ()
    assert [g.shape for g in groups] == list(SHAPE_ORDER)
    for g in groups:
        assert len(g.params) == len(g.roi_ids) == 1


def test_disabled_rois_skipped_not_invalid():
    rois = [_rect("off", 0, 0, 9, 9, enabled=False)]
    groups, invalid = build_shape_groups(rois, **CTX)
    assert groups == () and invalid == ()


def test_invalid_geometry_rejected_before_halcon():
    class Bad:
        roi_id = "bad"
        name = "bad"
        enabled = True

        class geometry:
            shape = ROIShape.CIRCLE
            parameters = {"center_y": 1.0, "center_x": 1.0, "radius": -5.0}

    groups, invalid = build_shape_groups([Bad()], **CTX)
    assert groups == ()
    assert len(invalid) == 1
    assert invalid[0].roi_id == "bad"
    assert "radius" in invalid[0].error


def test_degenerate_rectangle_rejected():
    class Bad:
        roi_id = "deg"
        name = "deg"
        enabled = True

        class geometry:
            shape = ROIShape.RECTANGLE1
            parameters = {"y1": 20, "x1": 20, "y2": 10, "x2": 30}

    groups, invalid = build_shape_groups([Bad()], **CTX)
    assert groups == ()
    assert len(invalid) == 1 and "degenerate" in invalid[0].error


def test_unsupported_shape_rejected():
    class Weird:
        roi_id = "w"
        name = "w"
        enabled = True

        class geometry:
            shape = "not-a-shape"
            parameters = {}

    groups, invalid = build_shape_groups([Weird()], **CTX)
    assert groups == ()
    assert len(invalid) == 1 and invalid[0].shape is None


def test_empty_roi_set_gives_empty_groups():
    groups, invalid = build_shape_groups([], **CTX)
    assert groups == () and invalid == ()


def test_view_is_immutable():
    rois = [_rect("r1", 0, 0, 9, 9)]
    groups, _ = build_shape_groups(rois, **CTX)
    with pytest.raises(Exception):
        groups[0].roi_ids = ("x",)  # frozen dataclass


# ---------------- Group B: RegionCache ----------------

def test_cache_initial_build_and_hit():
    from thermal_monitor.processing.halcon import HalconROIAdapter
    ad = _build_adapter()
    groups, _ = build_shape_groups([_rect("r1", 0, 0, 9, 9)], **CTX)
    assert ad.region_cache.lookup(groups[0]) is None  # miss
    regions = ad._build_group_regions(ha, groups[0])
    ad.region_cache.store(groups[0], regions)
    assert ha.count_obj(ad.region_cache.lookup(groups[0])) == 1  # hit
    assert ad.region_cache.stats.misses == 1
    assert ad.region_cache.stats.hits == 1
    assert ad.region_cache.stats.builds == 1


def test_cache_per_shape_dirty_rebuild():
    ad = _build_adapter()
    rois = [_rect("r1", 0, 0, 9, 9), _circle("c1", 50.0, 50.0, 10.0)]
    groups, _ = build_shape_groups(rois, **CTX)
    for g in groups:
        ad.region_cache.store(g, ad._build_group_regions(ha, g))
    # Change only the circle geometry.
    rois2 = [_rect("r1", 0, 0, 9, 9), _circle("c1", 60.0, 60.0, 10.0)]
    groups2, _ = build_shape_groups(rois2, **CTX)
    by_shape = {g.shape: g for g in groups2}
    assert ad.region_cache.lookup(by_shape[ROIShape.RECTANGLE1]) is not None
    assert ad.region_cache.lookup(by_shape[ROIShape.CIRCLE]) is None
    assert ad.region_cache.stats.rebuilds == 1


def test_cache_position_change_evicts():
    ad = _build_adapter()
    groups, _ = build_shape_groups([_rect("r1", 0, 0, 9, 9)], **CTX)
    ad.region_cache.store(groups[0], ad._build_group_regions(ha, groups[0]))
    evicted = ad.region_cache.prune_context(
        camera_id="cam1", position_id="p2", context_generation=3)
    assert evicted == 1
    assert len(ad.region_cache) == 0
    # Old position's regions are unreachable for the new position.
    assert ad.region_cache.lookup(groups[0]) is None


def test_cache_generation_change_misses():
    ad = _build_adapter()
    groups, _ = build_shape_groups([_rect("r1", 0, 0, 9, 9)], **CTX)
    ad.region_cache.store(groups[0], ad._build_group_regions(ha, groups[0]))
    groups_new, _ = build_shape_groups(
        [_rect("r1", 0, 0, 9, 9)],
        camera_id="cam1", position_id="p1", context_generation=4)
    assert ad.region_cache.lookup(groups_new[0]) is None


def test_cache_invalidate_camera_and_position():
    ad = _build_adapter()
    groups, _ = build_shape_groups([_rect("r1", 0, 0, 9, 9)], **CTX)
    ad.region_cache.store(groups[0], ad._build_group_regions(ha, groups[0]))
    assert ad.region_cache.invalidate_position("cam1", "other") == 0
    assert ad.region_cache.invalidate_position("cam1", "p1") == 1
    ad.region_cache.store(groups[0], ad._build_group_regions(ha, groups[0]))
    assert ad.region_cache.invalidate_camera("other") == 0
    assert ad.region_cache.invalidate_camera("cam1") == 1


def test_cache_clear_releases():
    ad = _build_adapter()
    groups, _ = build_shape_groups([_rect("r1", 0, 0, 9, 9)], **CTX)
    ad.region_cache.store(groups[0], ad._build_group_regions(ha, groups[0]))
    ad.region_cache.clear()
    assert len(ad.region_cache) == 0


def test_no_cross_worker_sharing():
    from thermal_monitor.processing.halcon import HalconROIAdapter
    ad1, ad2 = HalconROIAdapter(), HalconROIAdapter()
    assert ad1.region_cache is not ad2.region_cache
