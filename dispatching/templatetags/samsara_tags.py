"""Template tags for the Samsara live-tracking panel (dispatch leg rows).

The panel is a self-contained component: its state logic lives in
`dispatching.samsara_risk.build_panel_context` (called here), NOT in the row
template. The row template just does `{% samsara_tracking_panel leg %}`.
"""
from django import template

from dispatching.samsara_risk import build_panel_context

register = template.Library()


@register.inclusion_tag("dispatching/includes/_samsara_tracking_panel.html")
def samsara_tracking_panel(leg):
    """Render the live-tracking panel for a dispatch leg row (or nothing)."""
    return {"panel": build_panel_context(leg)}
