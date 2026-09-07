# -*- coding: utf-8 -*-
"""Special FAT landing handling when FDT and the first FAT are coincident."""

from math import hypot

from qgis.core import QgsMessageLog, QgsPointXY, Qgis

from . import cable_offset_layout_v6 as _v6

_INSTALLED = False
_ORIGINAL_TARGET_FOR_FAT = _v6._target_for_fat


def _log(message, level=Qgis.Info):
    try:
        QgsMessageLog.logMessage(str(message), "ODN_Tools_Pro / Cable Offset", level)
    except Exception:
        pass


def _same_point_segment(points, tolerance=1e-9):
    if not points:
        return False
    if len(points) == 1:
        return True
    try:
        x0, y0 = float(points[0][0]), float(points[0][1])
        return all(
            hypot(float(p[0]) - x0, float(p[1]) - y0) <= tolerance
            for p in points[1:]
        )
    except Exception:
        return False


def _segment_offset_sign(segment, work_crs, edge_crs, source_crs, spacing):
    """Return the first reliable non-zero left/right offset sign on a segment."""
    points = _v6._geometry_points(segment.get("points", []) or [])
    if len(points) < 2:
        return None

    edges, nodes = _v6._route_edges_nodes(segment, work_crs, source_crs, edge_crs)
    if not edges or len(nodes) != len(edges) + 1:
        return None

    work_points = _v6._transform_points(points, source_crs, work_crs)
    threshold = max(float(spacing) * 0.25, 0.01)

    for edge_index in range(len(edges)):
        a, b = nodes[edge_index], nodes[edge_index + 1]
        edge_len = hypot(b.x() - a.x(), b.y() - a.y())
        if edge_len <= 1e-9:
            continue

        tx, ty = _v6._unit(a, b)
        best_signed = None
        for p in work_points:
            along = (p.x() - a.x()) * tx + (p.y() - a.y()) * ty
            if -0.25 <= along <= edge_len + 0.25:
                signed = (p.x() - a.x()) * (-ty) + (p.y() - a.y()) * tx
                if best_signed is None or abs(signed) > abs(best_signed):
                    best_signed = signed

        if best_signed is None:
            continue
        snapped = round(float(best_signed) / float(spacing)) * float(spacing)
        if abs(snapped) >= threshold:
            _log(
                f"[fat-landing-coincident-side] offset={snapped:+.3f}m; "
                f"edge_index={edge_index}; side={'left' if snapped > 0 else 'right'}"
            )
            return float(snapped), edge_index

    return None


def _first_nonzero_offset_sign_on_link(design, pos, work_crs, edge_crs, source_crs, spacing):
    """Find only the left/right side from the first non-zero offset later in the Link.

    The actual FAT target is NOT taken from that later point. The target is
    generated at the coincident FDT position, exactly one control distance
    perpendicular to the local main-line direction.
    """
    segments = design.get("segments", []) or []
    start = max(0, int(pos))
    for seg_index in range(start, len(segments)):
        result = _segment_offset_sign(
            segments[seg_index], work_crs, edge_crs, source_crs, spacing
        )
        if result is not None:
            signed, edge_index = result
            return signed, seg_index, edge_index
    return None


def _local_mainline(ref, design, work_crs, edge_crs, source_crs):
    """Return the coincident FDT anchor and the local main-line tangent."""
    segments = design.get("segments", []) or []
    pos = int(ref.get("sequence_pos", -1))

    incoming_index = pos - 1 if 0 <= pos - 1 < len(segments) else None
    outgoing_index = pos if 0 <= pos < len(segments) else None

    incoming = segments[incoming_index] if incoming_index is not None else None
    outgoing = segments[outgoing_index] if outgoing_index is not None else None

    in_edges, in_nodes = _v6._route_edges_nodes(incoming, work_crs, source_crs, edge_crs)
    out_edges, out_nodes = _v6._route_edges_nodes(outgoing, work_crs, source_crs, edge_crs)

    # For FDT=FAT the incoming segment is the logical zero-length segment.
    # The actual main-line direction therefore comes from the outgoing route.
    if len(out_nodes) >= 2:
        anchor = QgsPointXY(out_nodes[0])
        tangent = _v6._unit(out_nodes[0], out_nodes[1])
        if hypot(tangent[0], tangent[1]) > 1e-12:
            return anchor, tangent, "outgoing"

    # Defensive fallback for an unusual sequence representation.
    if len(in_nodes) >= 2:
        anchor = QgsPointXY(in_nodes[-1])
        tangent = _v6._unit(in_nodes[-2], in_nodes[-1])
        if hypot(tangent[0], tangent[1]) > 1e-12:
            return anchor, tangent, "incoming"

    return None


def _coincident_target(ref, design, fat_feature, fat_layer, work_crs, edge_crs, spacing, control_distance):
    segments = design.get("segments", []) or []
    pos = int(ref.get("sequence_pos", -1))
    incoming_index = pos - 1 if 0 <= pos - 1 < len(segments) else None
    incoming = segments[incoming_index] if incoming_index is not None else None
    incoming_points = list(incoming.get("points", []) or []) if incoming else []

    if not _same_point_segment(incoming_points):
        return None

    source_crs = _v6._crs_from_authid(design.get("source_crs")) or edge_crs
    local = _local_mainline(ref, design, work_crs, edge_crs, source_crs)
    if local is None:
        _log(
            f"[fat-landing-coincident-skip] design={ref.get('design_index')}; "
            f"seq_pos={pos}; reason=无法确定FDT处主线方向",
            Qgis.Warning,
        )
        return None

    anchor, tangent, tangent_source = local

    # We only inspect the offset geometry to determine LEFT or RIGHT.
    # The landing distance itself is always the configured control distance.
    side_info = _first_nonzero_offset_sign_on_link(
        design, pos, work_crs, edge_crs, source_crs, float(spacing)
    )
    if side_info is None:
        _log(
            f"[fat-landing-coincident-skip] design={ref.get('design_index')}; "
            f"seq_pos={pos}; reason=无法确定后续线路偏移左右方向",
            Qgis.Warning,
        )
        return None

    side_signed, source_segment_index, source_edge_index = side_info
    distance = float(control_distance)
    signed_control = distance if side_signed > 0 else -distance
    target = _v6._offset_point(anchor, tangent, signed_control)

    _log(
        f"[fat-landing-coincident-target] design={ref.get('design_index')}; "
        f"seq_pos={pos}; tangent_source={tangent_source}; "
        f"side={'left' if side_signed > 0 else 'right'}; "
        f"control_distance={distance:.3f}m; "
        f"source_segment={source_segment_index}; source_edge={source_edge_index}; "
        "target=fixed perpendicular control-distance from coincident FDT"
    )

    return target, {
        "mode": "coincident_control",
        "turn_angle": 0.0,
        # Keep the real offset sign for diagnostics. The target distance is
        # controlled independently by control_distance.
        "offset": side_signed,
        "anchor": QgsPointXY(anchor),
        "side": "left" if side_signed > 0 else "right",
        "control_distance": distance,
    }


def _patched_target_for_fat(
    ref,
    design,
    fat_feature,
    fat_layer,
    work_crs,
    edge_crs,
    spacing,
    control_distance,
    corner_threshold_deg,
):
    target, info = _ORIGINAL_TARGET_FOR_FAT(
        ref,
        design,
        fat_feature,
        fat_layer,
        work_crs,
        edge_crs,
        spacing,
        control_distance,
        corner_threshold_deg,
    )

    fallback = _coincident_target(
        ref,
        design,
        fat_feature,
        fat_layer,
        work_crs,
        edge_crs,
        spacing,
        control_distance,
    )
    if fallback is None:
        return target, info

    fallback_target, fallback_info = fallback
    _log(
        f"[fat-landing-coincident-fallback] design={ref.get('design_index')}; "
        f"seq_pos={ref.get('sequence_pos')}; "
        "FDT/FAT initially coincident; FAT moved to fixed control-distance "
        "perpendicular landing point"
    )
    return fallback_target, fallback_info


def install_coincident_fat_fallback():
    global _INSTALLED
    if _INSTALLED:
        return
    _v6._target_for_fat = _patched_target_for_fat
    _INSTALLED = True
