"""Tests for the shared chart template module."""

import re
from pathlib import Path

import plotly.graph_objects as go

import chart_theme
from chart_theme import PALETTE, PLOTLY_CONFIG, style

ROOT = Path(__file__).resolve().parents[1]
HEX_RE = re.compile(r"#[0-9A-Fa-f]{6}")
LEGACY_HEXES = {"#F18F01", "#2E86AB", "#A23B72"}
# Quoted CSS colour names, not preceded by "[" so PALETTE["blue"] is allowed.
CSS_NAMES = (
    "red|orange|yellow|green|blue|purple|pink|brown|gray|grey|black|white"
    "|lightgreen|lightblue|lightgray|lightgrey|darkred|darkgreen|darkblue"
    "|steelblue|gold|silver|navy|teal|olive|maroon|lime|cyan|magenta|violet"
    "|coral|salmon|khaki|beige"
)
CSS_NAME_RE = re.compile(rf"""(?<!\[)["'](?:{CSS_NAMES})["']""", re.IGNORECASE)


def test_style_applies_shared_layout():
    fig = go.Figure()
    out = style(fig)
    assert out is fig
    # Plotly expands the template name into a full template object.
    import plotly.io as pio

    assert fig.layout.template == pio.templates["plotly_white"]
    assert fig.layout.margin.l == 40
    assert fig.layout.margin.r == 20
    assert fig.layout.margin.t == 70
    assert fig.layout.margin.b == 40
    assert fig.layout.hovermode == "x unified"
    assert fig.layout.legend.orientation == "h"
    assert fig.layout.legend.y == 1.0
    assert fig.layout.legend.yanchor == "bottom"


def test_title_never_collides_with_the_legend():
    """The title is pinned to the top of the figure, and a per-chart title
    text cannot drop that placement (the legend sits in the top margin)."""
    fig = go.Figure()
    style(fig, title="Weekly distance")
    assert fig.layout.title.text == "Weekly distance"
    assert fig.layout.title.yref == "container"
    assert fig.layout.title.yanchor == "top"
    assert fig.layout.title.y == 0.98


def test_style_caller_override_wins():
    fig = go.Figure()
    style(fig, hovermode="closest", margin={"l": 10})
    assert fig.layout.hovermode == "closest"
    assert fig.layout.margin.l == 10


def test_palette_values_are_unique_and_legacy_hexes_confined():
    assert len(set(PALETTE.values())) == len(PALETTE)
    for hex_code in LEGACY_HEXES:
        assert hex_code in (ROOT / "chart_theme.py").read_text()
    # Hex colour literals must stay out of the dashboard; the theme module is
    # scoped as the single owner (other files use named colours).
    assert HEX_RE.findall((ROOT / "dashboard.py").read_text()) == []


def test_dashboard_has_no_colour_literals():
    """Criterion 13: every colour comes from the theme, hex or CSS name."""
    source = (ROOT / "dashboard.py").read_text()
    offenders = HEX_RE.findall(source) + CSS_NAME_RE.findall(source)
    assert offenders == [], f"colour literals belong in chart_theme.py: {offenders}"


def test_render_passes_shared_config(monkeypatch):
    calls: dict = {}

    def fake_plotly_chart(fig, **kwargs):
        calls["fig"] = fig
        calls.update(kwargs)

    monkeypatch.setattr(chart_theme.st, "plotly_chart", fake_plotly_chart)
    fig = go.Figure()
    chart_theme.render(fig, width="stretch")
    assert calls["fig"] is fig
    assert calls["config"] == PLOTLY_CONFIG
    assert calls["width"] == "stretch"
