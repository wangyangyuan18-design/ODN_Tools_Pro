# -*- coding: utf-8 -*-
"""Pole Edge cable offset layout using 0.50 m lane spacing.

The active geometry/allocator is v10:
- complete-route sequential lane allocation;
- main lane is preferred whenever it is available;
- relative lane continuity is preserved across consecutive edges;
- new cables join existing groups from the outside;
- ordinary Pole nodes remain exclusive;
- ordinary corners do NOT use the 0.30 m control distance;
- 0.30 m is reserved for explicit same-point multi-cable fan-out/takeoff;
- FAT landing follows the final geometry of its owning Link.
"""

from qgis.PyQt.QtCore import QSettings
from qgis.core import QgsMessageLog, Qgis

from . import cable_offset_layout_v3 as _v3
from . import cable_offset_layout_v10 as _v10

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
    original_v3_geometry = _v3._build_explicit_points
    counters = {"edges": 0, "overlap_edges": 0, "slots": {}}
    try:
        # IMPORTANT: v3.apply_layout_to_designs internally installs its own
        # base._build_segment_points wrapper, but that wrapper calls the
        # module-local v3._build_explicit_points(). Therefore patching only
        # base._build_segment_points is NOT sufficient. This was the reason
        # the previous v10 geometry changes had almost no visible effect in
        # QGIS even though the v10 allocator logs appeared.
        global_assigner, lane_debug = _v10.make_global_slot_assigner(
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
            _log("[global-lane-priority-v10] " + ", ".join(route_text))
        _log(
            f"[global-lane-v10] links={len(ordered)}; "
            f"mapped_edges={len((lane_debug or {}).get('slot_map', {}))}; "
            "main_rule=available-main-first; route_sequential=ON"
        )

        base._assign_slots = global_assigner

        # v10 must become the geometry function actually called by v3.
        # v10's wrapper uses the v8 continuous-corner builder with explicit
        # endpoint semantics: normal Pole = direct landing, special shared
        # endpoint = 0.30 m takeoff.
        base._build_segment_points = _v10.make_build_wrapper(
            original_base_build, control_distance_m
        )
        _v3._build_explicit_points = _v10.make_build_wrapper(
            original_v3_geometry, control_distance_m
        )
        _log(
            "[geometry-v10] module-local v3 builder patched; "
            "endpoint semantics and continuous-corner geometry are ACTIVE"
        )

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
            summary["global_lane_allocator"] = lane_debug.get("allocator_version", "v10")
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
        _v3._build_explicit_points = original_v3_geometry


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
        "main_lane=available-main-first; route=sequential-continuity; "
        "corner=same_lane_continuous; lane_change=conflict_only; "
        "fat=owning_link_final_geometry"
    )

    summary = _apply(
        designs, distribution_layer, edge_layer, spacing, control_distance_m
    )

    for design in designs or []:
        layout = dict(design.get("layout") or {})
        layout["version"] = 17
        layout["spacing_m"] = round(spacing, 3)
        layout["corner_control_distance_m"] = round(control_distance_m, 3)
        layout["rule"] = "global_route_sequential_relative_lanes"
        layout["transition_angle"] = "geometry_from_offset_lane_spacing"
        layout["corner_geometry"] = "v10_continuous_offset_lanes"
        layout["main_lane_rule"] = "available_main_lane_first"
        layout["lane_continuity_rule"] = "preserve_previous_relative_lane"
        layout["group_join_rule"] = "new_cable_outside_existing_group"
        layout["pole_node_rule"] = "one_independent_cable_per_ordinary_pole"
        layout["fanout_control_rule"] = "same_point_special_nodes_only"
        layout["fat_landing"] = "owning_link_final_offset_geometry"
        layout.pop("angles_deg", None)
        design["layout"] = layout

    summary.pop("angles_deg", None)
    summary["corner_control_distance_m"] = control_distance_m
    summary["ordinary_corner_control_distance"] = None
    summary["fanout_control_distance_m"] = control_distance_m
    summary["transition_rule"] = "continuous relative-lane geometry; no ordinary 0.30m control"
    summary["corner_geometry"] = "v10_continuous_offset_lanes"
    summary["lane_allocator"] = "v10_route_sequential_main_first"
    summary["fat_landing"] = "owning_link_final_offset_geometry"
    _log(
        f"[result] changed={summary.get('changed_designs', 0)}; "
        f"extra={summary.get('extra_length_m', 0):.3f}m; "
        "ordinary_corner_control=NOT_USED; "
        f"same_point_fanout_control={control_distance_m:.3f}m; "
        "main_lane=available-main-first; fat=owning-link-final-geometry"
    )
    _log("========== Offset END ==========")
    return summary
