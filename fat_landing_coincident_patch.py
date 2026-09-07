# -*- coding: utf-8 -*-
"""Special FAT landing rule for a FAT initially coincident with its FDT.

For the FDT=FAT case, the incoming logical Segment has zero length. The FAT
must not remain at the original Pole Edge point merely because the first
outgoing edge is still at zero offset. Instead, scan the complete offset
geometry of the owning Link from the FDT forward and place the FAT at the
first point where that Link actually leaves the original Pole Edge position.
The ordinary FAT landing rules are unchanged.
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


def _first_nonzero_offset_point(design, start_segment_index, work_crs, edge_crs, source_crs, spacing):
    """Find the first real non-zero offset point along the Link after the FDT."""
    segments = design.get("segments", []) or []
    threshold = max(float(spacing) * 0.25, 0.01)

    for seg_index in range(max(0, int(start_segment_index)), len(segments)):
        segment = segments[seg_index] or {}
        points = _v6._geometry_points(segment.get("points", []) or [])
        if len(points) < 2:
            continue
        try:
            work_points = _v6._transform_points(points, source_crs, work_crs)
        except Exception:
            continue

        _, edge_nodes = _v6._route_edges_nodes(segment, work_crs, source_crs, edge_crs)
        if len(edge_nodes) < 2:
            continue

        # Inspect the generated offset points in route order. For each point,
        # estimate its signed lateral displacement from the closest original
        # Pole Edge segment. The first point with a material displacement is
        # the first place where the offset Link actually leaves the main route.
        for point_index, point in enumerate(work_points):
            best = None
            for edge_index in range(len(edge_nodes) - 1):
                a = edge_nodes[edge_index]
                b = edge_nodes[edge_index + 1]
                dx = b.x() - a.x()
                dy = b.y() - a.y()
                length_sq = dx * dx + dy * dy
                if length_sq <= 1e-18:
                    continue
                t = ((point.x() - a.x()) * dx + (point.y() - a.y()) * dy) / length_sq
                t = max(0.0, min(1.0, t))
                projection = QgsPointXY(a.x() + t * dx, a.y() + t * dy)
                ddx = point.x() - projection.x()
                ddy = point.y() - projection.y()
                dist_sq = ddx * ddx + ddy * ddy
                cross = dx * ddy - dy * ddx
                signed = cross / (length_sq ** 0.5)
                candidate = (dist_sq, abs(signed), signed, edge_index)
                if best is None or candidate[0] < best[0]:
                    best = candidate

            if best is None:
                continue
            _, magnitude, signed, edge_index = best
            if magnitude < threshold:
                continue

            _log(
                f"[fat-landing-coincident-search] segment={seg_index}; point={point_index}; "
                f"edge_index={edge_index}; offset={signed:+.3f}m; "
                "first_nonzero_offset_point=found"
            )
            return point, signed, seg_index, point_index

    return None


def _coincident_target(ref, design, work_crs, edge_crs, spacing):
    segments = design.get("segments", []) or []
    pos = int(ref.get("sequence_pos", -1))
    incoming_index = pos - 1 if 0 <= pos - 1 < len(segments) else None
    incoming = segments[incoming_index] if incoming_index is not None else None
    incoming_points = list(incoming.get("points", []) or []) if incoming else []
    if not _same_point_segment(incoming_points):
        return None

    # FAT immediately after FDT; begin searching from its outgoing Segment.
    outgoing_index = pos if 0 <= pos < len(segments) else None
    if outgoing_index is None:
        return None

    source_crs = _v6._crs_from_authid(design.get("source_crs")) or edge_crs
    found = _first_nonzero_offset_point(
        design,
        outgoing_index,
        work_crs,
        edge_crs,
        source_crs,
        float(spacing),
    )
    if found is None:
        return None

    target_work, signed, seg_index, point_index = found
    target_edge = _v6._transform_point(target_work, work_crs, edge_crs)

    # The target is an actual point on the generated offset geometry, not a
    # reconstructed point from the first Pole Edge direction. This preserves
    # the real transition/corner produced by the offset engine.
    return target_edge, {
        "mode": "coincident_first_offset",
        "turn_angle": 0.0,
        "offset": signed,
        "anchor": target_edge,
        "side": "first_nonzero_offset_point",
        "source_segment": seg_index,
        "source_point": point_index,
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

    # Only intervene for the exact special case: FAT is immediately after FDT
    # and the incoming Segment is geometrically coincident/zero-length.
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
        f"source_segment={fallback_info.get('source_segment')}; "
        f"source_point={fallback_info.get('source_point')}; "
        "FDT/FAT initially coincident; FAT moved to first nonzero offset point"
    )
    return fallback_target, fallback_info


def install_coincident_fat_fallback():
    global _INSTALLED
    if _INSTALLED:
        return
    _v6._target_for_fat = _patched_target_for_fat
    _INSTALLED = True
