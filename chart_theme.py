"""Shared visual template for every dashboard chart.

One palette, one layout template, one Plotly config: `dashboard.py` must not
carry its own hex literals or hand-tuned layouts.
"""

import plotly.graph_objects as go
import streamlit as st

ORANGE = "#F18F01"
BLUE = "#2E86AB"
MAGENTA = "#A23B72"
# Zone and reference colours are named, not hex: the bands they shade are
# semantic (in band / over-reaching / red-flag), so the name is the label.
GRAY = "gray"  # zero and reference lines
SAFE = "lightgreen"  # in-band zone shading
CAUTION = "orange"  # upper band, the 110% line
DANGER = "red"  # over the limit, the 130% line
PALETTE = {
    "orange": ORANGE,
    "blue": BLUE,
    "magenta": MAGENTA,
    "gray": GRAY,
    "safe": SAFE,
    "caution": CAUTION,
    "danger": DANGER,
}
PLOTLY_CONFIG = {"displayModeBar": False}

LAYOUT = {
    "template": "plotly_white",
    # t=70 reserves room for the title AND the legend, which sit above the
    # plot area; t=40 put them on top of each other.
    "margin": {"l": 40, "r": 20, "t": 70, "b": 40},
    "hovermode": "x unified",
    # y=1.0 with yanchor="bottom" puts the legend's bottom edge exactly on the
    # plot area's top edge, so the legend always sits in the top margin —
    # a paper-coordinate offset above 1.0 scales with the plot height and
    # eventually climbs into the title.
    "legend": {"orientation": "h", "yanchor": "bottom", "y": 1.0, "xanchor": "left", "x": 0},
}


def style(fig: go.Figure, **overrides) -> go.Figure:
    """Apply the shared layout. Caller overrides win over the shared values."""
    fig.update_layout(**{**LAYOUT, **overrides})
    # Title placement is applied after the merge, so a per-chart title text
    # cannot drop it and let the title drift into the legend.
    fig.update_layout(title={"yanchor": "top", "y": 0.98, "yref": "container"})
    return fig


def render(fig: go.Figure, **kwargs) -> None:
    """Draw a figure through the shared template and the shared Plotly config."""
    st.plotly_chart(fig, config=PLOTLY_CONFIG, **kwargs)
