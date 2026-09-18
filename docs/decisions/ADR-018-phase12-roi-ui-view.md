# ADR-018: Phase 12.4 — Single Authoritative ROI UI View

Status: Accepted
Date: 2026-09-18
Supplements: ADR-015 (session ownership), phase10-roi-technical-guide §9

## 1. Problem

The Configuration workspace had two ROI sources: the overlay rendered
the position-bound `RoiDefinition` session (`RoiEditor`), while the ROI
table read the legacy camera-global `AnalysisConfig` (`ROIConfig`,
position `"default"`). The table stayed empty whenever the session held
ROIs, selections lived in two places, and overlay labels showed only
`roi_id` for selected/alarm ROIs.

## 2. Decision

One authoritative UI snapshot per active camera+position: the
`RoiDefinition` list owned by the session `RoiEditor`. The table
(`ROIPanel.set_session_rois`), the overlay
(`RoiCanvasController.build_overlays`), and selection (canvas
`selected_id` <-> panel row, synced both ways through the window) all
derive from it. The legacy `AnalysisConfig` table path remains only for
the no-session state; its CRUD buttons are disabled while a session is
active (canvas toolbar owns create/delete; persistence still flows
through `_persist_roi_async` off the GUI thread).

Hit testing is shape-aware (rectangle exact, circle/ellipse normalized
boundary distance, polygon point-in-polygon + edge distance) with the
existing 6.0 image-px tolerance; ties resolve to the later (topmost)
item. Coordinate math stays in the shared `ViewportMapping`
(`mapping_for_widget` now the single construction used by both
`CameraRegion` and the configuration workspace).

## 3. Alternatives rejected

- Syncing the legacy table to the session by converting `ROIConfig`:
  rejected, keeps two models and the `"default"` fallback hazard.
- Building ROI UI into Live/Observer window: rejected for this phase —
  it hosts no ROI UI at all (verified: zero ROI references); that is a
  separate feature, not a fix.
- Session rename via the legacy property editor: rejected — it writes
  `AnalysisConfig`, the wrong store; rename stays canvas/editor-side
  only (documented limitation).

## 4. Consequences

- Table populates/clears with the session; stale rows impossible
  (atomic replace, selection validated, stale loads rejected).
- Every visible ROI shows its name label (roi_id fallback); labels are
  paint-only and never affect hit testing.
- No acquisition/processing/PTZ/SQL/alarm changes; no GUI-thread I/O
  added; Phase 12.3 batching untouched.
