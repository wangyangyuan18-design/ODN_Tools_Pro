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


def _first_nonzero_offset_on_link(design, pos, work_crs, edge_crs, source_crs, spacing):
    """Find the first real non-zero offset location after the coincident FDT/FAT."""
    segments = design.get("segments", []) or []
    threshold = max(float(spacing) * 0.25, 0.01)

    for seg_index in range(max(0, int(pos)), len(segments)):
        segment = segments[seg_index]
        points = _v6._geometry_points(segment.get("points", []) or [])
        if len(points) < 2:
            continue

        edges, nodes = _v6._route_edges_nodes(
            segment, work_crs, source_crs, edge_crs
        )
        if not edges or len(nodes) != len(edges) + 1:
            continue

        work_points = _v6._transform_points(points, source_crs, work_crs)
        slots = []
        for edge_index in range(len(edges)):
            a, b = nodes[edge_index], nodes[edge_index + 1]
            edge_len = hypot(b.x() - a.x(), b.y() - a.y())
            if edge_len <= 1e-9:
                slots.append((edge_index, 0.0))
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
                best_signed = 0.0
            snapped = round(float(best_signed) / float(spacing)) * float(spacing)
            slots.append((edge_index, float(snapped)))

        for edge_index, signed in slots:
            if abs(signed) < threshold:
                continue
            a, b = nodes[edge_index], nodes[edge_index + 1]
            tangent = _v6._unit(a, b)
            anchor = QgsPointXY(a)
            target = _v6._offset_point(anchor, tangent, signed)
            _log(
                f"[fat-landing-coincident-search] design=?; segment={seg_index}; "
                f"edge_index={edge_index}; offset={signed:+.3f}m; "
                "first_nonzero_offset_point=found"
            )
            return target, signed, seg_index, edge_index

    return None


def _coincident_target(ref, design, work_crs, edge_crs, spacing):
    segments = design.get("segments", []) or []
    pos = int(ref.get("sequence_pos", -1))
    incoming_index = pos - 1 if 0 <= pos - 1 < len(segments) else None
    incoming = segments[incoming_index] if incoming_index is not None else None
    incoming_points = list(incoming.get("points", []) or []) if incoming else []
    if not _same_point_segment(incoming_points):
        return None

    source_crs = _v6._crs_from_authid(design.get("source_crs")) or edge_crs
    result = _first_nonzero_offset_on_link(
        design, pos, work_crs, edge_crs, source_crs, float(spacing)
    )
    if result is None:
        return None

    target, signed, seg_index, edge_index = result
    return target, {
        "mode": "coincident_fallback",
        "turn_angle": 0.0,
        "offset": signed,
        "anchor": QgsPointXY(target),
        "side": "first_nonzero_offset_on_link",
        "source_segment_index": seg_index,
        "source_edge_index": edge_index,
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

    fallback = _coincident_target(ref, design, work_crs, edge_crs, spacing)
    if fallback is None:
        return target, info

    fallback_target, fallback_info = fallback
    try:
        existing_offset = abs(float(info.get("offset"))) if info and info.get("offset") is not None else 0.0
    except Exception:
        existing_offset = 0.0
    if target is not None and existing_offset > max(float(spacing) * 0.25, 0.01):
        return target, info

    _log(
        f"[fat-landing-coincident-fallback] design={ref.get('design_index')}; "
        f"seq_pos={ref.get('sequence_pos')}; offset={float(fallback_info['offset']):+.3f}m; "
        "FDT/FAT initially coincident; target derived from first nonzero offset on full Link"
    )
    return fallback_target, fallback_info


def install_coincident_fat_fallback():
    global _INSTALLED
    if _INSTALLED:
        return
    _v6._target_for_fat = _patched_target_for_fat
    _INSTALLED = True
