"""Shared visual template for every dashboard chart.

One palette, one layout template, one Plotly config: `dashboard.py` must not
carry its own hex literals or hand-tuned layouts.
"""

import plotly.graph_objects as go
import streamlit as st

ORANGE = "#F18F01"
BLUE = "#2E86AB"
MAGENTA = "#A23B72"
PALETTE = {"orange": ORANGE, "blue": BLUE, "magenta": MAGENTA}
PLOTLY_CONFIG = {"displayModeBar": False}

LAYOUT = {
    "template": "plotly_white",
    "margin": {"l": 40, "r": 20, "t": 40, "b": 40},
    "hovermode": "x unified",
    "legend": {"orientation": "h", "yanchor": "bottom", "y": 1.02, "xanchor": "left", "x": 0},
}


def style(fig: go.Figure, **overrides) -> go.Figure:
    """Apply the shared layout. Caller overrides win over the shared values."""
    fig.update_layout(**{**LAYOUT, **overrides})
    return fig


def render(fig: go.Figure, **kwargs) -> None:
    """Draw a figure through the shared template and the shared Plotly config."""
    st.plotly_chart(fig, config=PLOTLY_CONFIG, **kwargs)
