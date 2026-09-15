"""
ui.theme.stylesheet -- Central QSS builder.

:func:`build_stylesheet` renders the *entire* application stylesheet from
a single :class:`ThemeDefinition`.  It is the only place in the codebase
allowed to map semantic tokens to QSS rules.

Widget contract (see :mod:`thermal_monitor.ui.theme.properties`):

- ``QPushButton`` emphasis via ``setProperty("variant", ...)`` where
  variant is one of ``primary`` / ``secondary`` / ``accent`` /
  ``danger`` / ``outline`` / ``ghost``.  Buttons without a variant get
  the neutral ``outline`` look.
- ``QLabel`` text roles via ``setProperty("role", ...)``: ``title``,
  ``subtitle``, ``status``, ``strong``, ``muted``, ``mono``,
  ``readout``, ``viewfinder``.
- Connection / lifecycle / alarm state via ``setProperty("status",
  ...)`` on labels and indicators.
- Live-camera tile frames via ``setProperty("role", "tile")`` plus
  ``setProperty("tileState", ...)``.
- Toolbar chrome via ``setProperty("role", "toolbar")``.
- Unsaved-changes emphasis via ``setProperty("dirty", True)`` on the
  save button.

Design rules: flat colors only (no gradients), no shadows, no
animations, restrained borders and corner radii so the stylesheet stays
cheap to apply during continuous 8-camera operation.
"""

from __future__ import annotations

from thermal_monitor.ui.theme.tokens import ThemeDefinition


def build_stylesheet(defn: ThemeDefinition, font_scale_pct: int = 100) -> str:
    """Render the full application QSS for *defn*.

    ``font_scale_pct`` scales the font/metric tokens at generation time
    (global Settings -> Font Size): the same single stylesheet, just
    larger type and taller controls — never a second hardcoded patch.
    """
    from thermal_monitor.ui.theme.fonts import scaled_font_px

    m = defn.metrics
    bw = m.border_width
    ff = m.font_family
    fm = m.font_mono
    pct = font_scale_pct
    fs_base = scaled_font_px(m.font_size_base, pct)
    fs_title = scaled_font_px(m.font_size_title, pct)
    fs_subtitle = scaled_font_px(m.font_size_subtitle, pct)
    fs_sm = scaled_font_px(m.font_size_sm, pct)
    from thermal_monitor.ui.theme.fonts import PANEL_TITLE_FONT_PT, SHELF_TAB_FONT_PT

    fs_shelf_tab = scaled_font_px(SHELF_TAB_FONT_PT, pct)
    fs_panel_title = scaled_font_px(PANEL_TITLE_FONT_PT, pct)
    control_min_h = scaled_font_px(m.control_height - m.button_padding_v, pct)
    btn_pad_v = scaled_font_px(m.button_padding_v, pct, floor=2)
    inp_pad_v = scaled_font_px(m.input_padding_v, pct, floor=2)
    return f"""
/* ===== TMS V3 theme: {defn.name} ({defn.display_name}) ===== */
/* Generated centrally -- do not scatter QSS across widgets. */
QWidget {{
    color: {defn.text};
    background-color: {defn.background};
    font-family: {ff};
    font-size: {fs_base}px;
}}
QMainWindow {{
    background-color: {defn.background};
}}
QDialog {{
    background-color: {defn.background};
}}
QMessageBox {{
    background-color: {defn.background};
}}
QMessageBox QLabel {{
    color: {defn.text};
}}
QToolTip {{
    color: {defn.text};
    background-color: {defn.surface_alt};
    border: {bw}px solid {defn.border_strong};
    padding: 4px;
}}

/* ----- labels: semantic roles ----- */
QLabel[role="title"] {{
    font-size: {fs_title}px;
    font-weight: bold;
    color: {defn.title};
}}
QLabel[role="subtitle"] {{
    font-size: {fs_subtitle}px;
    color: {defn.text_secondary};
}}
QLabel[role="status"] {{
    color: {defn.text_secondary};
    font-size: {fs_sm}px;
}}
QLabel[role="strong"] {{
    font-weight: bold;
    color: {defn.text};
}}
QLabel[role="muted"] {{
    color: {defn.muted_text};
    font-size: {fs_sm}px;
}}
QLabel[role="mono"] {{
    font-family: {fm};
    color: {defn.text_secondary};
}}
QLabel[role="readout"] {{
    font-family: {fm};
    font-size: {fs_sm}px;
    font-weight: bold;
    color: {defn.text};
}}
QLabel[role="viewfinder"] {{
    border: {bw}px solid {defn.border};
    background-color: {defn.surface_alt};
    color: {defn.muted_text};
}}

/* ----- labels/indicators: connection & alarm status ----- */
QLabel[status="connected"], QLabel[status="acquiring"],
QLabel[status="running"], QLabel[status="live"],
QLabel[status="ok"], QLabel[status="active"] {{
    color: {defn.camera_connected};
    font-weight: bold;
}}
QLabel[status="connecting"], QLabel[status="ready"] {{
    color: {defn.info};
    font-weight: bold;
}}
QLabel[status="disconnected"], QLabel[status="not_available"],
QLabel[status="unavailable"], QLabel[status="inactive"] {{
    color: {defn.camera_disconnected};
    font-weight: bold;
}}
QLabel[status="degraded"], QLabel[status="warning"],
QLabel[status="starting"], QLabel[status="reconnecting"] {{
    color: {defn.camera_warning};
    font-weight: bold;
}}
QLabel[status="error"], QLabel[status="alarm"] {{
    color: {defn.camera_error};
    font-weight: bold;
}}

/* ----- camera tile frames ----- */
QWidget[role="tile"] {{
    background-color: {defn.surface};
    border: {bw}px solid {defn.border};
    border-radius: {m.radius_md}px;
}}
QWidget[role="tile"][tileState="ready"] {{
    border-color: {defn.border_strong};
}}
QWidget[role="tile"][tileState="starting"] {{
    border-color: {defn.camera_warning};
}}
QWidget[role="tile"][tileState="reconnecting"] {{
    border-color: {defn.camera_warning};
}}
QWidget[role="tile"][tileState="running"] {{
    border-color: {defn.camera_connected};
}}
QWidget[role="tile"][tileState="error"] {{
    border-color: {defn.camera_error};
}}
QWidget[role="tile"][tileState="not_available"] {{
    border-color: {defn.border};
}}

/* ----- toolbar chrome ----- */
QWidget[role="toolbar"] {{
    background-color: {defn.surface};
    border-bottom: {bw}px solid {defn.border};
}}
QWidget[shelfRail="true"] {{
    background-color: transparent;
    border: none;
}}
QScrollArea[shelfScroll="true"] {{
    background-color: transparent;
    border: none;
}}
QScrollArea[shelfScroll="true"] QWidget {{
    background-color: transparent;
}}

/* ----- side-panel headers (Configuration shelves) ----- */
QWidget[panelHeader="true"] {{
    background-color: {defn.surface_alt};
    border-bottom: {bw}px solid {defn.border};
}}
QLabel[panelTitle="true"] {{
    font-size: {fs_panel_title}px;
    font-weight: bold;
}}

/* ----- side-shelf floating tabs (Configuration shelves) ----- */
/* Shelf-specific light-industrial tool-strip look (fixed palette so the
   shelf stays light and readable in every theme; the global theme and
   accent are untouched): very light neutral tab, subtle grey border,
   small dark-charcoal 12 pt text, restrained 4 px corners (industrial,
   not pill-shaped). Weight stays normal in every state so the advance
   never changes after selection. min-height/min-width 0 so the fixed
   tab geometry (PANEL_* constants) is never clamped by the generic
   button rule. The tab itself is custom-painted to match; these rules
   keep the QSS state identical for any non-painted context. */
QPushButton[shelfTab="true"] {{
    font-size: {fs_shelf_tab}px;
    font-weight: normal;
    background-color: #EFF1F4;
    color: #23272C;
    border: 1px solid #BCC4CC;
    border-radius: 4px;
    padding: 2px;
    min-height: 0px;
    min-width: 0px;
}}
QPushButton[shelfTab="true"]:hover {{
    background-color: #DFE4EA;
    border-color: #BCC4CC;
    color: #23272C;
}}
QPushButton[shelfTab="true"]:pressed {{
    background-color: #607D8B;
    border-color: #607D8B;
    color: #FFFFFF;
}}

/* ----- shelf pin buttons (icon-only, state carried by the icon) ----- */
QPushButton[pinButton="true"] {{
    background-color: transparent;
    border: none;
    border-radius: 2px;
    padding: 0px;
    min-height: 0px;
    min-width: 0px;
}}
QPushButton[pinButton="true"]:hover {{
    background-color: #DFE4EA;
}}
QPushButton[pinButton="true"]:pressed {{
    background-color: #CBD2D9;
}}

/* ----- buttons ----- */
QPushButton {{
    background-color: {defn.surface};
    color: {defn.text};
    border: {bw}px solid {defn.border};
    border-radius: {m.radius_md}px;
    padding: {btn_pad_v}px {m.spacing_md}px;
    min-height: {control_min_h}px;
}}
QPushButton:hover {{
    border-color: {defn.focus_ring};
}}
QPushButton:pressed {{
    background-color: {defn.surface_alt};
}}
QPushButton:disabled {{
    background-color: {defn.surface};
    color: {defn.muted_text};
    border-color: {defn.border};
}}
QPushButton[variant="primary"] {{
    background-color: {defn.primary};
    color: {defn.text_on_filled};
    border: none;
    font-weight: bold;
}}
QPushButton[variant="primary"]:hover {{
    background-color: {defn.primary_hover};
}}
QPushButton[variant="primary"]:pressed {{
    background-color: {defn.primary_pressed};
}}
QPushButton[variant="primary"]:disabled {{
    background-color: {defn.primary_disabled_bg};
    color: {defn.primary_disabled_text};
}}
QPushButton[variant="secondary"] {{
    background-color: {defn.secondary};
    color: {defn.text_on_filled};
    border: none;
    font-weight: bold;
}}
QPushButton[variant="secondary"]:hover {{
    background-color: {defn.secondary_hover};
}}
QPushButton[variant="secondary"]:pressed {{
    background-color: {defn.secondary_pressed};
}}
QPushButton[variant="secondary"]:disabled {{
    background-color: {defn.secondary_disabled_bg};
    color: {defn.secondary_disabled_text};
}}
QPushButton[variant="accent"] {{
    background-color: {defn.accent};
    color: {defn.text_on_filled};
    border: none;
    font-weight: bold;
}}
QPushButton[variant="accent"]:hover {{
    background-color: {defn.accent_hover};
}}
QPushButton[variant="accent"]:pressed {{
    background-color: {defn.accent_pressed};
}}
QPushButton[variant="accent"]:disabled {{
    background-color: {defn.surface};
    color: {defn.muted_text};
}}
QPushButton[variant="danger"] {{
    background-color: {defn.danger};
    color: {defn.text_on_filled};
    border: none;
    font-weight: bold;
}}
QPushButton[variant="danger"]:hover {{
    background-color: {defn.danger};
    border: {bw}px solid {defn.border_strong};
}}
QPushButton[variant="danger"]:pressed {{
    background-color: {defn.alarm};
}}
QPushButton[variant="danger"]:disabled {{
    background-color: {defn.surface};
    color: {defn.muted_text};
}}
QPushButton[variant="outline"] {{
    background-color: {defn.surface};
    color: {defn.text};
    border: {bw}px solid {defn.border};
}}
QPushButton[variant="outline"]:hover {{
    background-color: {defn.surface_alt};
    border-color: {defn.focus_ring};
}}
QPushButton[variant="outline"]:disabled {{
    color: {defn.muted_text};
}}
QPushButton[variant="ghost"] {{
    background-color: transparent;
    color: {defn.accent};
    border: {bw}px solid {defn.accent};
}}
QPushButton[variant="ghost"]:hover {{
    background-color: {defn.accent};
    color: {defn.text_on_filled};
}}
QPushButton[variant="ghost"]:disabled {{
    background-color: transparent;
    color: {defn.disabled};
    border-color: {defn.disabled};
}}
QPushButton[dirty="true"] {{
    background-color: {defn.warning};
    color: {defn.text_on_warning};
    border: none;
    font-weight: bold;
}}
QPushButton[dirty="true"]:hover {{
    background-color: {defn.warning};
    border: {bw}px solid {defn.border_strong};
}}

/* ----- side-shelf selected/collapsed (higher specificity wins) ----- */
/* Open tabs are physically hidden while their panel is open, so the
   accent rule below only shows transiently (press) — a muted
   steel-blue matching the custom paint. Minimized (outline/ghost)
   tabs keep the floating light-tab look. Weight stays normal in both
   so selecting a tab never changes its advance. */
QPushButton[shelfTab="true"][variant="accent"] {{
    background-color: #607D8B;
    color: #FFFFFF;
    border: 1px solid #607D8B;
    font-weight: normal;
}}
QPushButton[shelfTab="true"][variant="accent"]:hover {{
    background-color: #546E7A;
    border-color: #546E7A;
}}
QPushButton[shelfTab="true"][variant="outline"] {{
    background-color: #EFF1F4;
    color: #23272C;
    border: 1px solid #BCC4CC;
    font-weight: normal;
}}
QPushButton[shelfTab="true"][variant="outline"]:hover {{
    background-color: #DFE4EA;
    border-color: #BCC4CC;
}}
QPushButton[shelfTab="true"][variant="ghost"] {{
    background-color: #EFF1F4;
    color: #23272C;
    border: 1px solid #BCC4CC;
    font-weight: normal;
}}
QPushButton[shelfTab="true"][variant="ghost"]:hover {{
    background-color: #DFE4EA;
    border-color: #BCC4CC;
    color: #23272C;
}}
QPushButton[shelfTab="true"]:hover {{
    border-color: {defn.focus_ring};
}}
/* Thin shelf-rail scrollbars: the tab list scrolls instead of
   compressing names; 8 px keeps the 32 px tab fully readable. */
QScrollArea[shelfScroll="true"] QScrollBar:vertical {{
    width: 8px;
}}

/* ----- tool buttons ----- */
QToolButton {{
    background-color: transparent;
    border: {bw}px solid transparent;
    border-radius: {m.radius_md}px;
    padding: {btn_pad_v}px {m.spacing_md}px;
    color: {defn.text};
}}
QToolButton:hover {{
    background-color: {defn.surface_alt};
}}
QToolButton:pressed, QToolButton:checked {{
    background-color: {defn.secondary};
    color: {defn.text_on_filled};
}}
QToolButton:disabled {{
    color: {defn.muted_text};
}}

/* ----- inputs ----- */
QLineEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background-color: {defn.surface};
    color: {defn.text};
    border: {bw}px solid {defn.border};
    border-radius: {m.radius_sm}px;
    padding: {inp_pad_v}px {m.spacing_sm}px;
    selection-background-color: {defn.secondary};
    selection-color: {defn.text_on_filled};
}}
QLineEdit:focus, QTextEdit:focus, QSpinBox:focus,
QDoubleSpinBox:focus, QComboBox:focus {{
    border-color: {defn.focus_ring};
}}
QLineEdit:disabled, QTextEdit:disabled, QSpinBox:disabled,
QDoubleSpinBox:disabled, QComboBox:disabled {{
    background-color: {defn.surface};
    color: {defn.muted_text};
}}
QLineEdit:read-only, QTextEdit:read-only {{
    background-color: {defn.surface_alt};
    color: {defn.muted_text};
}}
QComboBox::drop-down {{
    border: none;
    width: 20px;
}}
QComboBox::down-arrow {{
    image: none;
    border-left: 5px solid transparent;
    border-right: 5px solid transparent;
    border-top: 5px solid {defn.text};
    margin-right: 5px;
}}
QComboBox QAbstractItemView {{
    background-color: {defn.surface};
    color: {defn.text};
    border: {bw}px solid {defn.border};
    selection-background-color: {defn.secondary};
    selection-color: {defn.text_on_filled};
}}

/* ----- check boxes / radio buttons ----- */
QCheckBox, QRadioButton {{
    spacing: {m.spacing_sm}px;
    color: {defn.text};
}}
QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border: {bw}px solid {defn.border_strong};
    border-radius: {m.radius_sm}px;
    background-color: {defn.surface};
}}
QCheckBox::indicator:checked {{
    background-color: {defn.secondary};
    border-color: {defn.secondary};
}}
QCheckBox::indicator:disabled {{
    background-color: {defn.surface_alt};
    border-color: {defn.border};
}}
QRadioButton::indicator {{
    width: 14px;
    height: 14px;
    border: {bw}px solid {defn.border_strong};
    border-radius: 7px;
    background-color: {defn.surface};
}}
QRadioButton::indicator:checked {{
    background-color: {defn.secondary};
    border-color: {defn.secondary};
}}

/* ----- sliders ----- */
QSlider::groove:horizontal {{
    border: {bw}px solid {defn.border};
    height: 8px;
    background: {defn.surface};
    border-radius: {m.radius_sm}px;
}}
QSlider::handle:horizontal {{
    background: {defn.secondary};
    border: 1px solid {defn.secondary};
    width: 16px;
    margin: -5px 0;
    border-radius: 8px;
}}
QSlider::handle:horizontal:hover {{
    background: {defn.secondary_hover};
}}
QSlider::handle:horizontal:disabled {{
    background: {defn.disabled};
    border-color: {defn.disabled};
}}
QSlider::groove:vertical {{
    border: {bw}px solid {defn.border};
    width: 8px;
    background: {defn.surface};
    border-radius: {m.radius_sm}px;
}}
QSlider::handle:vertical {{
    background: {defn.secondary};
    border: 1px solid {defn.secondary};
    height: 16px;
    margin: 0 -5px;
    border-radius: 8px;
}}

/* ----- tabs ----- */
QTabWidget::pane {{
    border: {bw}px solid {defn.border};
    background-color: {defn.background};
}}
QTabBar::tab {{
    background-color: {defn.surface};
    color: {defn.text};
    border: {bw}px solid {defn.border};
    border-bottom: none;
    border-top-left-radius: {m.radius_md}px;
    border-top-right-radius: {m.radius_md}px;
    padding: {m.spacing_sm}px {m.spacing_lg}px;
    margin-right: 2px;
}}
QTabBar::tab:selected {{
    background-color: {defn.background};
    border-bottom: 1px solid {defn.background};
    color: {defn.title};
}}
QTabBar::tab:hover:!selected {{
    background-color: {defn.surface_alt};
}}

/* ----- tables / trees / lists ----- */
QTableWidget, QTableView, QTreeWidget, QListWidget {{
    background-color: {defn.surface};
    alternate-background-color: {defn.surface_alt};
    border: {bw}px solid {defn.border};
    gridline-color: {defn.border};
    selection-background-color: {defn.secondary};
    selection-color: {defn.text_on_filled};
}}
QTableWidget::item, QTreeWidget::item, QListWidget::item {{
    padding: 4px;
}}
QHeaderView::section {{
    background-color: {defn.surface_alt};
    color: {defn.text};
    border: {bw}px solid {defn.border};
    padding: 6px;
    font-weight: bold;
}}

/* ----- group boxes ----- */
QGroupBox {{
    border: {bw}px solid {defn.border};
    border-radius: 2px;
    margin-top: {m.spacing_md}px;
    padding-top: 10px;
    font-weight: bold;
    background-color: {defn.surface};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 7px;
    padding: 0 4px;
    color: {defn.text};
    background-color: {defn.surface};
}}

/* ----- scroll areas / scroll bars ----- */
QScrollArea {{
    border: none;
    background-color: {defn.background};
}}
QScrollBar:vertical {{
    background-color: {defn.surface};
    width: 8px;
    border: none;
}}
QScrollBar::handle:vertical {{
    background-color: {defn.border_strong};
    border-radius: 6px;
    min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{
    background-color: {defn.muted_text};
}}
QScrollBar:horizontal {{
    background-color: {defn.surface};
    height: 8px;
    border: none;
}}
QScrollBar::handle:horizontal {{
    background-color: {defn.border_strong};
    border-radius: 6px;
    min-width: 30px;
}}
QScrollBar::handle:horizontal:hover {{
    background-color: {defn.muted_text};
}}
QScrollBar::add-line, QScrollBar::sub-line {{
    border: none;
    background: none;
}}

/* ----- progress bars ----- */
QProgressBar {{
    border: {bw}px solid {defn.border};
    border-radius: {m.radius_md}px;
    background-color: {defn.surface};
    text-align: center;
    color: {defn.text};
}}
QProgressBar::chunk {{
    background-color: {defn.secondary};
    border-radius: {m.radius_sm}px;
}}

/* ----- menus / toolbars / status ----- */
QMenuBar {{
    background-color: {defn.surface};
    color: {defn.text};
}}
QMenuBar::item:selected {{
    background-color: {defn.surface_alt};
}}
QMenu {{
    background-color: {defn.surface};
    color: {defn.text};
    border: {bw}px solid {defn.border};
}}
QMenu::item:selected {{
    background-color: {defn.secondary};
    color: {defn.text_on_filled};
}}
QMenu::item:disabled {{
    color: {defn.muted_text};
}}
QToolBar {{
    background-color: {defn.surface};
    border-bottom: {bw}px solid {defn.border};
    spacing: {m.spacing_xs}px;
}}
QStatusBar {{
    background-color: {defn.surface};
    color: {defn.text_secondary};
    border-top: {bw}px solid {defn.border};
}}
QStatusBar QLabel {{
    color: {defn.text_secondary};
}}

/* ----- splitters / frames ----- */
QSplitter::handle {{
    background-color: {defn.border};
}}
QSplitter::handle:horizontal {{
    width: 2px;
}}
QSplitter::handle:vertical {{
    height: 2px;
}}
QFrame {{
    border-color: {defn.border};
}}
"""


__all__ = ["build_stylesheet"]
