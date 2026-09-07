# -*- coding: utf-8 -*-
"""Special FAT landing handling when FDT and the first FAT are coincident.

Only the FDT=FAT case is handled here. Normal FAT landing logic remains in
cable_offset_layout_v6 unchanged.
"""

from math import hypot

from qgis.core import QgsGeometry, QgsMessageLog, QgsPointXY, Qgis

from . import cable_offset_layout_v6 as _v6

_INSTALLED = False
_ORIGINAL_TARGET_FOR_FAT = _v6._target_for_fat
_ORIGINAL_REPLACE_FAT_ENDPOINTS = _v6._replace_fat_endpoints


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


def _local_mainline(ref, design, work_crs, edge_crs, source_crs):
    """Return the FDT anchor and local Pole Edge direction."""
    segments = design.get("segments", []) or []
    pos = int(ref.get("sequence_pos", -1))
    outgoing_index = pos if 0 <= pos < len(segments) else None
    incoming_index = pos - 1 if 0 <= pos - 1 < len(segments) else None

    outgoing = segments[outgoing_index] if outgoing_index is not None else None
    incoming = segments[incoming_index] if incoming_index is not None else None

    _, out_nodes = _v6._route_edges_nodes(outgoing, work_crs, source_crs, edge_crs)
    if len(out_nodes) >= 2:
        tangent = _v6._unit(out_nodes[0], out_nodes[1])
        if hypot(tangent[0], tangent[1]) > 1e-12:
            return QgsPointXY(out_nodes[0]), tangent, "outgoing"

    _, in_nodes = _v6._route_edges_nodes(incoming, work_crs, source_crs, edge_crs)
    if len(in_nodes) >= 2:
        tangent = _v6._unit(in_nodes[-2], in_nodes[-1])
        if hypot(tangent[0], tangent[1]) > 1e-12:
            return QgsPointXY(in_nodes[-1]), tangent, "incoming"
    return None


def _intersection_points(geometry):
    result = []
    if geometry is None or geometry.isEmpty():
        return result
    try:
        if geometry.type() == 0:
            if geometry.isMultipart():
                result.extend(QgsPointXY(p) for p in geometry.asMultiPoint())
            else:
                result.append(QgsPointXY(geometry.asPoint()))
        elif geometry.type() == 1:
            parts = geometry.asMultiPolyline() if geometry.isMultipart() else [geometry.asPolyline()]
            for part in parts:
                result.extend(QgsPointXY(p) for p in part)
        elif geometry.isMultipart():
            for part in geometry.asGeometryCollection():
                result.extend(_intersection_points(part))
    except Exception:
        pass
    return result


def _outgoing_offset_intersection(ref, design, work_crs, edge_crs, source_crs, control_distance):
    """Find FAT on the already-generated outgoing offset Link.

    A perpendicular is positioned exactly ``control_distance`` metres along
    the local main-line direction from the coincident FDT. The FAT is the
    intersection with the actual outgoing offset geometry, so no artificial
    direction or artificial side branch is created.
    """
    segments = design.get("segments", []) or []
    pos = int(ref.get("sequence_pos", -1))
    outgoing_index = pos if 0 <= pos < len(segments) else None
    incoming_index = pos - 1 if 0 <= pos - 1 < len(segments) else None
    outgoing = segments[outgoing_index] if outgoing_index is not None else None
    incoming = segments[incoming_index] if incoming_index is not None else None

    incoming_points = list(incoming.get("points", []) or []) if incoming else []
    if not _same_point_segment(incoming_points) or outgoing is None:
        return None

    local = _local_mainline(ref, design, work_crs, edge_crs, source_crs)
    if local is None:
        _log(
            f"[fat-landing-coincident-skip] design={ref.get('design_index')}; "
            f"seq_pos={pos}; reason=无法确定FDT处主线方向",
            Qgis.Warning,
        )
        return None

    anchor, tangent, tangent_source = local
    along_point = QgsPointXY(
        anchor.x() + tangent[0] * float(control_distance),
        anchor.y() + tangent[1] * float(control_distance),
    )
    nx, ny = -tangent[1], tangent[0]
    span = max(10.0, float(control_distance) * 20.0)
    p1 = QgsPointXY(along_point.x() - nx * span, along_point.y() - ny * span)
    p2 = QgsPointXY(along_point.x() + nx * span, along_point.y() + ny * span)
    perpendicular = QgsGeometry.fromPolylineXY([p1, p2])

    raw_points = _v6._geometry_points(outgoing.get("points", []) or [])
    if len(raw_points) < 2:
        return None
    route_points = _v6._transform_points(raw_points, source_crs, work_crs)
    route_geom = QgsGeometry.fromPolylineXY(route_points)
    if route_geom.isEmpty():
        return None

    candidates = _intersection_points(route_geom.intersection(perpendicular))
    if candidates:
        candidates.sort(key=lambda p: hypot(p.x() - along_point.x(), p.y() - along_point.y()))
        target = candidates[0]
        mode = "perpendicular_intersection"
    else:
        nearest = route_geom.nearestPoint(QgsGeometry.fromPointXY(along_point))
        if nearest.isEmpty():
            return None
        target = QgsPointXY(nearest.asPoint())
        mode = "nearest_on_link"

    _log(
        f"[fat-landing-coincident-link-point] design={ref.get('design_index')}; "
        f"seq_pos={pos}; tangent_source={tangent_source}; "
        f"control_distance={float(control_distance):.3f}m; mode={mode}; "
        f"target=({target.x():.3f},{target.y():.3f})"
    )
    return target, {
        "mode": "coincident_on_link",
        "turn_angle": 0.0,
        "offset": None,
        "anchor": QgsPointXY(anchor),
        "control_distance": float(control_distance),
        "coincident_on_link": True,
    }


def _coincident_target(ref, design, fat_feature, fat_layer, work_crs, edge_crs, spacing, control_distance):
    segments = design.get("segments", []) or []
    pos = int(ref.get("sequence_pos", -1))
    incoming_index = pos - 1 if 0 <= pos - 1 < len(segments) else None
    incoming = segments[incoming_index] if incoming_index is not None else None
    incoming_points = list(incoming.get("points", []) or []) if incoming else []
    if not _same_point_segment(incoming_points):
        return None
    source_crs = _v6._crs_from_authid(design.get("source_crs")) or edge_crs
    return _outgoing_offset_intersection(
        ref, design, work_crs, edge_crs, source_crs, float(control_distance)
    )


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
        "FDT/FAT initially coincident; FAT target taken directly from actual "
        "outgoing offset Link geometry"
    )
    return fallback_target, fallback_info


def _project_point_on_segment(point, a, b):
    dx = b.x() - a.x()
    dy = b.y() - a.y()
    length2 = dx * dx + dy * dy
    if length2 <= 1e-18:
        return QgsPointXY(a), 0.0
    t = ((point.x() - a.x()) * dx + (point.y() - a.y()) * dy) / length2
    t = max(0.0, min(1.0, t))
    return QgsPointXY(a.x() + t * dx, a.y() + t * dy), t


def _trim_outgoing_to_coincident_target(designs, moves, edge_crs):
    """Trim the old FDT prefix so the next Link segment starts at FAT."""
    for move in moves.values():
        if not move.get("coincident_on_link"):
            continue
        di = int(move["design_index"])
        pos = int(move["sequence_pos"])
        if di < 0 or di >= len(designs):
            continue
        segments = designs[di].get("segments", []) or []
        if pos < 0 or pos >= len(segments):
            continue
        segment = segments[pos]
        raw = list(segment.get("points", []) or [])
        if len(raw) < 2:
            continue

        source_crs = _v6._crs_from_authid(designs[di].get("source_crs")) or edge_crs
        raw_xy = _v6._geometry_points(raw)
        # Work in the Pole Edge CRS here because move["target_edge"] is in
        # that CRS. This also avoids assuming that the Link source CRS uses
        # metres.
        edge_points = _v6._transform_points(raw_xy, source_crs, edge_crs)
        target_edge = QgsPointXY(move["target_edge"])

        best_index = 0
        best_distance = float("inf")
        for i in range(len(edge_points) - 1):
            projected, _ = _project_point_on_segment(target_edge, edge_points[i], edge_points[i + 1])
            d = hypot(projected.x() - target_edge.x(), projected.y() - target_edge.y())
            if d < best_distance:
                best_distance = d
                best_index = i

        target_src = _v6._transform_point(target_edge, edge_crs, source_crs)
        suffix = [[float(target_src.x()), float(target_src.y())]]
        suffix.extend(
            [float(p[0]), float(p[1])] for p in raw[best_index + 1:]
        )

        cleaned = []
        for p in suffix:
            if not cleaned or hypot(
                float(p[0]) - float(cleaned[-1][0]),
                float(p[1]) - float(cleaned[-1][1]),
            ) > 1e-9:
                cleaned.append(p)
        if len(cleaned) < 2:
            continue

        segment["points"] = cleaned
        segment["distance"] = _v6._polyline_distance(cleaned, source_crs)
        segment["zero_length"] = False
        _log(
            f"[fat-landing-coincident-trim] design={di}; seq_pos={pos}; "
            f"removed_prefix_segments={best_index}; continuation=FAT"
        )


def _patched_replace_fat_endpoints(designs, moves, edge_crs):
    changed = _ORIGINAL_REPLACE_FAT_ENDPOINTS(designs, moves, edge_crs)
    _trim_outgoing_to_coincident_target(designs, moves, edge_crs)
    return changed


def install_coincident_fat_fallback():
    global _INSTALLED
    if _INSTALLED:
        return
    _v6._target_for_fat = _patched_target_for_fat
    _v6._replace_fat_endpoints = _patched_replace_fat_endpoints
    _INSTALLED = True
