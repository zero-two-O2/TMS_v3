# ADR-019: Phase 12.5 — Industrial Toolbar Icons and Transient Preview

Status: Accepted
Date: 2026-09-18
Supplements: ADR-018 (single UI view)

## 1. Icons

Toolbar glyphs are original line-art SVGs authored for this
application (`ui/resources/icons/*.svg`, 24x24 viewBox, uniform 1.8px
stroke), loaded via QtSvg in `roi_icons.icon_for` with the legacy
QPainter glyphs kept strictly as a missing-asset fallback. No
third-party or ThermoView assets are used or copied; the style is only
inspired by compact industrial instrumentation toolbars. One registry
(`ICON_KEYS` + `icon_path` + `icon_source`), one toolbar definition
(`TOOL_GROUPS`), uniform 20px icons on 28px buttons.

## 2. Transient preview state

Drawing preview lives in `RoiCanvasController` (`_preview` press/current
+ `_hover` cursor) and renders through the existing `ROIOverlay` paint
path with `preview=True` (dashed cyan pen, no label). It never enters
the editor, table, pipeline, alarms, or persistence; commit still flows
exclusively through `interaction.click` + `editor.end_drag` on release.
`preview_geometry` on the interaction state reuses the same finishers
as `click()`, so preview and commit cannot diverge. Generation-guarded
per frame of movement; cleared on commit, Escape, tool switch, and any
editor replacement (camera/position switch).

## 3. Fixes folded in

- Coldest Spot creation was silently dead (`click()` had no branch for
  it although the model expects the search-region box): added to the
  box branch producing the model's `HottestSpotGeometry`.
- No rotated-rectangle drawing tool exists (toolbar offers axis-aligned
  Rectangle only): rotation during creation is not faked; Rectangle2
  remains supported for loaded geometries, not for drawing.
- Painter gained an open-`polyline` branch (line tools, angle segments,
  polygon rubber band, cross arms); `SPOT`/`NOTE` commit on click and
  have no drag preview by construction.

## 4. Consequences

Preview repaints on mouse move only (Qt `update`, no synchronous
paints); no DB/HALCON work during moves; frames never touch canvas
state. All Phase 12.4 behaviors preserved and re-tested.

## 5. Phase 12.5.1 optical sizing addendum

Root cause of tiny-looking icons: artwork margins, not Qt or button
padding. Measured (96px offscreen render, alpha bbox): dominant
dimensions 50–86%, thin symbols ~14%, area 13–77%. Fix: redrew all 28
assets at 24px viewBox with artwork spanning ~2–22 units (≈1px
clearance), stroke 2.0–2.4 (details 1.4–1.6), filled dots/arrowheads
for small-size presence. Re-measured: dominant dimensions 66–92%
(thin 1-D symbols span ~90% on their long axis by construction).
`availableSizes()` stays empty for scalable SVGs — non-null QIcon is
the load gate. Button geometry unchanged (existing 30px icons / 40px
buttons kept; whitespace is not fixed by scaling chrome).

## 6. Phase 12.5.2 maximum-occupancy redesign

Why 12.5.1 still looked small: strokes stayed thin (1.8–2.1), fills
sparse, and several symbols sat at 50–70% frame. Redesign: artwork
1–23 units, primary stroke 2.4–2.8 (details 1.6–1.8), filled cursor/
dots/heads/pupil, larger arrowheads and endpoint markers. Re-measured
dominant dimensions 64–96% (thin 1-D tools ~90%+ on the long axis).

Contrast discovery: the default theme is industrial_dark (surface
#1E232A), so dark-ink glyphs were near-invisible. `icon_for` gained an
`ink` variant — "light" (default) recolors asset ink to #ECEFF3 via
byte replacement rendered through QSvgRenderer at a 192px master;
"dark" loads the file as authored for light surfaces. `RoiToolbar`
takes `ink="light"` by default. No runtime theme-change tracking (no
theme signal exists) — documented limitation. Toolbar dimensions
untouched (40/40 on disk, not modified by this phase).

Test caveat found and fixed: QImage ARGB32 memory order is B,G,R,A on
little-endian — pixel assertions must index channels [2,1,0].
Contact sheets: `reports/phase12_5_2_contact_sheet_{dark,light}_40px.png`
and `_dark_16px.png`, visually reviewed (bold, framed, legible at
16px); automated gates (SVG source, sizes 16–28px, ≥65% dominant,
transparent frame, WCAG ≥2.5 vs dark surface) all pass.

## 7. Phase 12.5.3 dropdown separation and theme switching

Root cause of overlap: 40px button + 40px icon + 12px style menu zone
painted over the artwork (measured: zone at x=28, icon centered in the
full rect — padding does not move it, verified pixel-identical).
Fix: menu buttons are 52px wide with the 12px arrow zone composed into
the pixmap itself (`dropdown_icon_for`: 40px art left-aligned +
transparent strip; style indicator paints only transparent pixels).
Height/spacing unchanged; standalone buttons stay 40px.

Theme switching: `ThemeManager.apply_and_refresh` now refreshes every
widget exposing duck-typed `refresh_theme_icons(surface_hex)` (no
import cycle); `RoiToolbar` re-resolves all button + menu icons from
the active surface luminance (`ink_for_surface`, >0.4 → dark ink).
New toolbars default via `default_toolbar_ink()` parsed from the live
application stylesheet. Checked buttons get Normal/On light-ink
pixmaps (blue `secondary` in every theme); Disabled is Qt-automatic.
No runtime theme signal exists — the apply funnel is the single hook.
Sheets: `reports/phase12_5_3_toolbar_{dark,light}.png`,
`phase12_5_3_popup_{dark,light}.png` (popup text renders as boxes
offscreen — environment lacks fonts, verified with bare QLabel).
