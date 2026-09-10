# -*- coding: utf-8 -*-
"""FAT landing logic coordinated with the final offset Cable geometry.

The FAT does not follow the old 0.30 m control-point fallback. After cable
layout is generated, the final Distribution Cable geometry is authoritative:
- straight FATs land on the nearest point of the actual offset route;
- corner FATs land on the nearest point of the actual generated corner route;
- the FAT remains associated with its Link/sequence, so a FAT served by L6
  follows L6 even when its original pole was visually associated with L4.
"""

from math import hypot

from qgis.core import QgsGeometry, QgsPointXY

from . import cable_offset_layout_v6 as _v6


def _geometry_points(points):
    return [QgsPointXY(float(p[0]), float(p[1])) for p in points if len(p) >= 2]


def _transform_points(points, source_crs, target_crs):
    return [_v6._transform_point(p, source_crs, target_crs) for p in points]


def _nearest_on_segments(segments, anchor, source_crs, work_crs):
    candidates = []
    for label, segment in segments:
        if not segment:
            continue
        raw = _geometry_points(segment.get("points", []) or [])
        if len(raw) < 2:
            continue
        pts = _transform_points(raw, source_crs, work_crs)
        geom = QgsGeometry.fromPolylineXY(pts)
        if geom.isEmpty():
            continue
        nearest = geom.nearestPoint(QgsGeometry.fromPointXY(anchor))
        if nearest.isEmpty():
            continue
        p = QgsPointXY(nearest.asPoint())
        d = hypot(p.x() - anchor.x(), p.y() - anchor.y())
        candidates.append((d, label, p))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], 0 if item[1] == "incoming" else 1))
    return candidates[0]


def target_for_fat(ref, design, fat_feature, fat_layer, work_crs, edge_crs, spacing, control_distance, corner_threshold_deg):
    segments = design.get("segments", []) or []
    pos = int(ref["sequence_pos"])
    incoming_index = pos - 1 if 0 <= pos - 1 < len(segments) else None
    outgoing_index = pos if 0 <= pos < len(segments) else None
    incoming = segments[incoming_index] if incoming_index is not None else None
    outgoing = segments[outgoing_index] if outgoing_index is not None else None

    in_edges, in_nodes = _v6._route_edges_nodes(incoming, work_crs, edge_crs, edge_crs)
    out_edges, out_nodes = _v6._route_edges_nodes(outgoing, work_crs, edge_crs, edge_crs)
    anchor = QgsPointXY(in_nodes[-1]) if in_nodes else (
        QgsPointXY(out_nodes[0]) if out_nodes else _v6._feature_point_in_work(fat_feature, fat_layer, work_crs)
    )
    if anchor is None:
        return None, {"reason": "无法确定 FAT 所属 Pole Edge 节点"}

    chosen = _nearest_on_segments(
        [("incoming", incoming), ("outgoing", outgoing)],
        anchor,
        edge_crs,
        work_crs,
    )
    if chosen is None:
        return None, {"reason": "无法从最终 Cable 几何确定 FAT 落点"}

    distance, side, target = chosen
    if in_nodes and out_nodes:
        is_corner, turn = _v6._classify_corner(in_nodes, out_nodes, float(corner_threshold_deg))
    elif out_nodes:
        is_corner, turn = _v6._classify_endpoint_corner(out_nodes, True, float(corner_threshold_deg))
    elif in_nodes:
        is_corner, turn = _v6._classify_endpoint_corner(in_nodes, False, float(corner_threshold_deg))
    else:
        is_corner, turn = False, 0.0

    mode = "corner_actual_offset_geometry" if is_corner else "straight_actual_offset_geometry"
    _v6._log(
        f"[fat-landing-v2] fid={fat_feature.id()}; design={ref['design_index']}; "
        f"seq_pos={pos}; mode={mode}; source={side}; distance_to_pole={distance:.3f}m; "
        f"control_distance={float(control_distance):.3f}m; spacing={float(spacing):.3f}m"
    )
    return target, {
        "mode": mode,
        "turn_angle": turn,
        "offset": None,
        "anchor": anchor,
        "control_distance": float(control_distance),
        "source": side,
        "geometry_distance": distance,
    }
