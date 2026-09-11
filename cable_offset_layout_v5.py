# -*- coding: utf-8 -*-
"""Pole Edge cable offset layout using 0.50 m lane spacing.

The active geometry/allocator is v9:
- main-lane priority follows directional continuity first;
- same-lane corners stay continuous and parallel;
- ordinary corners do NOT use the 0.30 m control distance;
- 0.30 m is reserved for same-point multi-cable fan-out/takeoff logic
  (FDT/BB/CL/FAT return), not for normal Pole Edge lane transitions;
- FAT landing follows the actual final offset Cable geometry.
"""

from qgis.PyQt.QtCore import QSettings
from qgis.core import QgsMessageLog, Qgis

from . import cable_offset_layout_v3 as _v3
from . import cable_offset_layout_v9 as _v9

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
        control_distance = float(QSettings().value(CONTROL_DISTANCE_KEY, DEFAULT_CONTROL_DISTANCE_M))
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


def _apply(designs, distribution_layer, edge_layer, spacing, control_distance_m):
    base = _v3._v2._base
    original_base_assign = base._assign_slots
    original_base_build = base._build_segment_points
    counters = {"edges": 0, "overlap_edges": 0, "slots": {}}
    try:
        global_assigner, lane_debug = _v9.make_global_slot_assigner(
            designs, distribution_layer, edge_layer, spacing, counters
        )
        ordered = lane_debug.get("ordered_designs", []) if lane_debug else []
        if ordered:
            route_text = []
            routes = lane_debug.get("routes", {}) or {}
            for rank, di in enumerate(ordered[:12], start=1):
                route = routes.get(di, {})
                route_text.append(
                    f"#{rank}=design{di}:score={float(route.get('priority_score', 0.0)):.1f};"
                    f"run={float(route.get('longest_directional_run', 0.0)):.1f}m;"
                    f"len={float(route.get('route_length', 0.0)):.1f}m;"
                    f"turns={int(route.get('turn_count', 0))}"
                )
            _log("[global-lane-priority-v9] " + ", ".join(route_text))
        _log(
            f"[global-lane-v9] links={len(ordered)}; "
            f"mapped_edges={len((lane_debug or {}).get('slot_map', {}))}; "
            "main_rule=directional_continuity_then_length"
        )

        base._assign_slots = global_assigner
        # v9 now owns ordinary corner geometry and deliberately does not use
        # control_distance_m for Pole Edge turns.
        base._build_segment_points = _v9.make_build_wrapper(
            original_base_build, control_distance_m
        )

        # Do not inject the 0.30 m value into the generic transition factory.
        # This prevents ordinary Pole Edge corners from being treated as
        # control-distance lane changes. The 0.30 m value remains available to
        # dedicated same-point fan-out/takeoff logic (FDT/BB/CL/FAT return).
        original_factory_local = _v3._transition_factory
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

        try:
            from . import cable_offset_layout_v6 as _v6_runtime
            from . import cable_offset_fat_v2 as _fat_v2
            _v6_runtime._target_for_fat = _fat_v2.target_for_fat
            _log("[fat-landing-v2] active: FAT follows final offset Cable geometry")
        except Exception as exc:
            _log(
                f"[fat-landing-v2] activation failed: {type(exc).__name__}: {exc}",
                Qgis.Warning,
            )

        if lane_debug:
            summary["global_lane_priority"] = list(ordered)
            summary["global_lane_slot_map"] = dict(lane_debug.get("slot_map", {}))
            summary["global_lane_route_lengths"] = {
                str(di): round(float(data.get("route_length", 0.0)), 3)
                for di, data in (lane_debug.get("routes", {}) or {}).items()
            }
            summary["global_lane_metrics"] = {
                str(di): {
                    "priority_score": round(float(data.get("priority_score", 0.0)), 3),
                    "longest_directional_run": round(float(data.get("longest_directional_run", 0.0)), 3),
                    "turn_count": int(data.get("turn_count", 0)),
                    "reversal_count": int(data.get("reversal_count", 0)),
                }
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
        f"[rule] spacing={spacing:.3f}m; ordinary_corner_control=NOT_USED; "
        f"same_point_fanout_control={control_distance_m:.3f}m; "
        "main_lane=directional_continuity; corner=same_lane_continuous; "
        "lane_change=offset_lane_geometry; fat=actual_offset_geometry"
    )

    summary = _apply(
        designs, distribution_layer, edge_layer, spacing, control_distance_m
    )

    for design in designs or []:
        layout = dict(design.get("layout") or {})
        layout["version"] = 15
        layout["spacing_m"] = round(spacing, 3)
        layout["corner_control_distance_m"] = round(control_distance_m, 3)
        layout["rule"] = "global_directional_priority_continuous_lanes"
        layout["transition_angle"] = "geometry_from_offset_lane_spacing"
        layout["corner_geometry"] = "v9_continuous_offset_lanes"
        layout["main_lane_rule"] = "longest_directional_run_then_route_length"
        layout["fanout_control_rule"] = "same_point_only"
        layout["fat_landing"] = "actual_final_offset_cable_geometry"
        layout.pop("angles_deg", None)
        design["layout"] = layout

    summary.pop("angles_deg", None)
    summary["corner_control_distance_m"] = control_distance_m
    summary["ordinary_corner_control_distance"] = None
    summary["fanout_control_distance_m"] = control_distance_m
    summary["transition_rule"] = "offset-lane geometry; no 0.30m ordinary-corner control"
    summary["corner_geometry"] = "v9_continuous_offset_lanes"
    summary["fat_landing"] = "actual_final_offset_cable_geometry"
    _log(
        f"[result] changed={summary.get('changed_designs', 0)}; "
        f"extra={summary.get('extra_length_m', 0):.3f}m; "
        "ordinary_corner_control=NOT_USED; "
        f"same_point_fanout_control={control_distance_m:.3f}m; "
        "main_lane=directional_continuity; fat=actual_offset_geometry"
    )
    _log("========== Offset END ==========")
    return summary
