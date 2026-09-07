# -*- coding: utf-8 -*-
"""Fallback for FATs coincident with the Link's FDT.

When FDT and the first FAT share one coordinate, the incoming Segment can
contain only one point, so the normal FAT landing calculation has no incoming
Pole Edge geometry to measure. In that case the owning Link's first outgoing
segment is the source of truth: derive its actual lateral offset from the
first Pole Edge direction and place the FAT at that offset from the common
FDT/Pole anchor.
"""

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
        for p in points[1:]:
            if hypot(float(p[0]) - x0, float(p[1]) - y0) > tolerance:
                return False
        return True
    except Exception:
        return False


def _fallback_signed_offset(segment, edge_nodes, work_crs, source_crs, spacing):
    """Estimate the actual lateral offset of the outgoing offset geometry."""
    if not segment or len(edge_nodes) < 2:
        return None
    points = _v6._geometry_points(segment.get("points", []) or [])
    if len(points) < 2:
        return None
    try:
        work_points = _v6._transform_points(points, source_crs, work_crs)
    except Exception:
        return None

    a, b = edge_nodes[0], edge_nodes[1]
    tx, ty = _v6._unit(a, b)
    if abs(tx) <= 1e-12 and abs(ty) <= 1e-12:
        return None
    edge_len = hypot(b.x() - a.x(), b.y() - a.y())
    if edge_len <= 1e-9:
        return None

    # Prefer points whose projection falls on the first Pole Edge. This keeps
    # the sample on the first offset segment instead of accidentally using a
    # later corner with a different direction.
    samples = []
    for p in work_points:
        along = (p.x() - a.x()) * tx + (p.y() - a.y()) * ty
        if -0.25 <= along <= edge_len + 0.25:
            signed = (p.x() - a.x()) * (-ty) + (p.y() - a.y()) * tx
            samples.append(abs(float(signed)), float(signed))

    if not samples:
        return None

    # Use the strongest non-trivial lateral displacement on the first edge.
    samples.sort(key=lambda item: item[0], reverse=True)
    threshold = max(float(spacing) * 0.25, 0.01)
    for _, signed in samples:
        if abs(signed) < threshold:
            continue
        snapped = float(round(signed / float(spacing)) * float(spacing))
        if abs(snapped) >= threshold:
            return snapped
    return None


def _coincident_target(ref, design, work_crs, edge_crs, spacing):
    segments = design.get("segments", []) or []
    pos = int(ref.get("sequence_pos", -1))
    incoming_index = pos - 1 if 0 <= pos - 1 < len(segments) else None
    outgoing_index = pos if 0 <= pos < len(segments) else None
    incoming = segments[incoming_index] if incoming_index is not None else None
    outgoing = segments[outgoing_index] if outgoing_index is not None else None
    incoming_points = list(incoming.get("points", []) or []) if incoming else []
    if not _same_point_segment(incoming_points):
        return None
    if outgoing is None:
        return None

    _, out_nodes = _v6._route_edges_nodes(outgoing, work_crs, edge_crs, edge_crs)
    if len(out_nodes) < 2:
        return None

    signed = _fallback_signed_offset(
        outgoing,
        out_nodes,
        work_crs,
        edge_crs,
        float(spacing),
    )
    if signed is None:
        return None

    anchor = QgsPointXY(out_nodes[0])
    tangent = _v6._unit(out_nodes[0], out_nodes[1])
    target = _v6._offset_point(anchor, tangent, signed)
    return target, {
        "mode": "coincident_fallback",
        "turn_angle": 0.0,
        "offset": signed,
        "anchor": anchor,
        "side": "outgoing_fallback",
    }


def _patched_target_for_fat(ref, design, fat_feature, fat_layer, work_crs, edge_crs, spacing, control_distance, corner_threshold_deg):
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

    # Only intervene for the exact failure mode: the FAT is the first node
    # after FDT and the incoming Segment is geometrically coincident.
    fallback = _coincident_target(ref, design, work_crs, edge_crs, spacing)
    if fallback is None:
        return target, info

    fallback_target, fallback_info = fallback
    if target is not None:
        try:
            existing_offset = abs(float(info.get("offset"))) if info and info.get("offset") is not None else 0.0
        except Exception:
            existing_offset = 0.0
        if existing_offset > max(float(spacing) * 0.25, 0.01):
            return target, info

    _log(
        f"[fat-landing-coincident-fallback] design={ref.get('design_index')}; "
        f"seq_pos={ref.get('sequence_pos')}; offset={float(fallback_info['offset']):+.3f}m; "
        "FDT/FAT initially coincident; target derived from outgoing Link offset"
    )
    return fallback_target, fallback_info


def install_coincident_fat_fallback():
    global _INSTALLED
    if _INSTALLED:
        return
    _v6._target_for_fat = _patched_target_for_fat
    _INSTALLED = True
