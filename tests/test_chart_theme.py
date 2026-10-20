"""Tests for the shared chart template module."""

import re
from pathlib import Path

import plotly.graph_objects as go

import chart_theme
from chart_theme import PALETTE, PLOTLY_CONFIG, style

ROOT = Path(__file__).resolve().parents[1]
HEX_RE = re.compile(r"#[0-9A-Fa-f]{6}")
LEGACY_HEXES = {"#F18F01", "#2E86AB", "#A23B72"}


def test_style_applies_shared_layout():
    fig = go.Figure()
    out = style(fig)
    assert out is fig
    # Plotly expands the template name into a full template object.
    import plotly.io as pio

    assert fig.layout.template == pio.templates["plotly_white"]
    assert fig.layout.margin.l == 40
    assert fig.layout.margin.r == 20
    assert fig.layout.margin.t == 40
    assert fig.layout.margin.b == 40
    assert fig.layout.hovermode == "x unified"
    assert fig.layout.legend.orientation == "h"


def test_style_caller_override_wins():
    fig = go.Figure()
    style(fig, hovermode="closest", margin={"l": 10})
    assert fig.layout.hovermode == "closest"
    assert fig.layout.margin.l == 10


def test_palette_values_are_unique_and_legacy_hexes_confined():
    assert len(set(PALETTE.values())) == 3
    for hex_code in LEGACY_HEXES:
        assert hex_code in (ROOT / "chart_theme.py").read_text()
    # Hex colour literals must stay out of the dashboard; the theme module is
    # scoped as the single owner (other files use named colours).
    assert HEX_RE.findall((ROOT / "dashboard.py").read_text()) == []


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
