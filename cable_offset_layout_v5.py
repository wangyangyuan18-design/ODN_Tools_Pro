# -*- coding: utf-8 -*-
"""Pole Edge cable offset layout using a fixed corner control distance.

The offset spacing is the lateral separation between parallel cable runs.
The corner/transition is no longer controlled by a selectable angle.
Instead, the transition run along the Pole Edge is fixed by
``control_distance_m`` (default 0.30 m). The resulting geometric angle is
only a consequence of the two distances:

    angle = atan(offset_change / control_distance_m)

Therefore 45/60/75/90 degrees are not input parameters anymore.

Important geometry rule:
- Same-lane corners are built by ``cable_offset_layout_v8`` as one continuous
  offset polyline using a miter/bevel join.
- The 0.30 m control distance is used only for genuine lane changes.
- A same-lane corner never returns to the original Pole Edge node just to
  leave that node again.
"""

from math import atan, degrees

from qgis.PyQt.QtCore import QSettings
from qgis.core import QgsMessageLog, Qgis

from . import cable_offset_layout_v3 as _v3
from . import cable_offset_layout_v7 as _v7
from . import cable_offset_layout_v8 as _v8

DEFAULT_SPACING_M = 0.5
DEFAULT_CONTROL_DISTANCE_M = 0.30
SPACING_KEY = "ODNToolsPro/CableOffsetLayout/spacing_m"
CONTROL_DISTANCE_KEY = "ODNToolsPro/CableOffsetLayout/control_distance_m"
LOG_TAG = "ODN_Tools_Pro / Cable Offset"


def _log(message, level=Qgis.Info):
    try:
        QgsMessageLog.logMessage(str(message), LOG_TAG, level)
    except Exception:
        pass


def get_settings():
    try:
        spacing = float(QSettings().value(SPACING_KEY, DEFAULT_SPACING_M))
    except Exception:
        spacing = DEFAULT_SPACING_M
    try:
        control_distance = float(
            QSettings().value(CONTROL_DISTANCE_KEY, DEFAULT_CONTROL_DISTANCE_M)
        )
    except Exception:
        control_distance = DEFAULT_CONTROL_DISTANCE_M
    return max(0.01, spacing), max(0.01, control_distance)


def save_settings(spacing, control_distance):
    spacing = max(0.01, float(spacing))
    control_distance = max(0.01, float(control_distance))
    settings = QSettings()
    settings.setValue(SPACING_KEY, spacing)
    settings.setValue(CONTROL_DISTANCE_KEY, control_distance)
    settings.sync()


def _natural_transition_factory(control_distance_m, spacing):
    """Return a transition-angle function whose angle is derived, not chosen."""
    control_distance_m = max(0.01, float(control_distance_m))
    spacing = max(0.01, float(spacing))

    def transition(change):
        offset_change = abs(float(change)) * spacing
        if offset_change <= 1e-12:
            return 0.0
        return degrees(atan(offset_change / control_distance_m))

    return transition


def _apply(designs, distribution_layer, edge_layer, spacing, control_distance_m):
    """Run the validated layout engine with global lanes and clean corners."""
    base = _v3._v2._base
    original_base_assign = base._assign_slots
    original_base_build = base._build_segment_points

    counters = {"edges": 0, "overlap_edges": 0, "slots": {}}
    try:
        global_assigner, lane_debug = _v7.make_global_slot_assigner(
            designs,
            distribution_layer,
            edge_layer,
            spacing,
            counters,
        )

        ordered = lane_debug.get("ordered_designs", []) if lane_debug else []
        if ordered:
            route_text = []
            routes = lane_debug.get("routes", {}) or {}
            for rank, di in enumerate(ordered[:12], start=1):
                route = routes.get(di, {})
                route_text.append(
                    f"#{rank}=design{di}:{float(route.get('route_length', 0.0)):.1f}m"
                )
            _log("[global-lane-priority] " + ", ".join(route_text))
        _log(
            f"[global-lane] links={len(ordered)}; "
            f"mapped_edges={len((lane_debug or {}).get('slot_map', {}))}"
        )

        base._assign_slots = global_assigner
        # v8 is the only active geometry builder. Same-lane corners are
        # continuous miter/bevel joins; no forced return to the Pole Edge node.
        base._build_segment_points = _v8.make_build_wrapper(original_base_build)

        # The transition angle is still derived from the control distance, but
        # this is used only when a Link changes lane, never at same-lane corners.
        original_factory_local = _v3._transition_factory
        _v3._transition_factory = lambda _ignored_angles: _natural_transition_factory(
            control_distance_m, spacing
        )
        try:
            summary = _v3.apply_explicit_layout_to_designs(
                designs,
                distribution_layer,
                edge_layer,
                spacing=spacing,
                angles=(90.0,),
            )
        finally:
            _v3._transition_factory = original_factory_local

        if lane_debug:
            summary["global_lane_priority"] = list(ordered)
            summary["global_lane_slot_map"] = dict(lane_debug.get("slot_map", {}))
            summary["global_lane_route_lengths"] = {
                str(di): round(float(data.get("route_length", 0.0)), 3)
                for di, data in (lane_debug.get("routes", {}) or {}).items()
            }
        return summary
    finally:
        base._assign_slots = original_base_assign
        base._build_segment_points = original_base_build


def apply_explicit_layout_to_designs(
    designs,
    distribution_layer,
    edge_layer,
    spacing=DEFAULT_SPACING_M,
    control_distance_m=DEFAULT_CONTROL_DISTANCE_M,
):
    try:
        spacing = max(0.01, float(spacing))
    except Exception:
        spacing = DEFAULT_SPACING_M
    try:
        control_distance_m = max(0.01, float(control_distance_m))
    except Exception:
        control_distance_m = DEFAULT_CONTROL_DISTANCE_M

    _log("========== Offset START ==========")
    _log(
        f"[rule] spacing={spacing:.3f}m; control_distance={control_distance_m:.3f}m; "
        "angles=derived_only; lane_policy=global_priority; "
        "corner_builder=v8_continuous_no_backtrack"
    )
    for magnitude in range(1, 9):
        offset = magnitude * spacing
        angle = degrees(atan(offset / control_distance_m))
        _log(
            f"[derived-angle] offset={offset:.3f}m; "
            f"control_distance={control_distance_m:.3f}m; angle={angle:.3f}deg"
        )

    summary = _apply(
        designs,
        distribution_layer,
        edge_layer,
        spacing,
        control_distance_m,
    )

    for design in designs or []:
        layout = dict(design.get("layout") or {})
        layout["version"] = 11
        layout["spacing_m"] = round(spacing, 3)
        layout["corner_control_distance_m"] = round(control_distance_m, 3)
        layout["rule"] = "global_link_priority_continuous_lanes"
        layout["transition_angle"] = "derived_from_offset_and_control_distance"
        layout["corner_geometry"] = "v8_miter_or_bevel_no_backtrack"
        layout.pop("angles_deg", None)
        design["layout"] = layout

    summary.pop("angles_deg", None)
    summary["corner_control_distance_m"] = control_distance_m
    summary["transition_rule"] = "derived_angle_atan(offset_change/control_distance)"
    summary["corner_geometry"] = "v8_miter_or_bevel_no_backtrack"
    _log(
        f"[result] changed={summary.get('changed_designs', 0)}; "
        f"extra={summary.get('extra_length_m', 0):.3f}m; "
        f"control_distance={control_distance_m:.3f}m; "
        "corner_builder=v8_continuous_no_backtrack"
    )
    _log("========== Offset END ==========")
    return summary
