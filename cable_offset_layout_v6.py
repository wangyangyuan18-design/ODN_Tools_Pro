# -*- coding: utf-8 -*-
"""Cable offset + FAT landing-point layout coordinated with Link Design."""

from math import acos, degrees, hypot

from qgis.PyQt.QtCore import QSettings
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsMessageLog,
    QgsPointXY,
    QgsProject,
    Qgis,
)

from . import cable_offset_layout_v5 as _v5

DEFAULT_SPACING_M = _v5.DEFAULT_SPACING_M
DEFAULT_CONTROL_DISTANCE_M = _v5.DEFAULT_CONTROL_DISTANCE_M
SPACING_KEY = "ODNToolsPro/CableOffsetLayout/spacing_m"
CONTROL_DISTANCE_KEY = "ODNToolsPro/CableOffsetLayout/control_distance_m"
DEFAULT_CORNER_THRESHOLD_DEG = 20.0
DEFAULT_FAT_MAX_DISTANCE_M = 3.0
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


def _crs_from_authid(authid):
    if not authid:
        return None
    try:
        crs = QgsCoordinateReferenceSystem(str(authid))
        return crs if crs.isValid() else None
    except Exception:
        return None


def _unit(a, b):
    dx = float(b.x()) - float(a.x())
    dy = float(b.y()) - float(a.y())
    length = hypot(dx, dy)
    if length <= 1e-12:
        return 0.0, 0.0
    return dx / length, dy / length


def _left_normal(tangent):
    return -tangent[1], tangent[0]


def _offset_point(point, tangent, distance):
    nx, ny = _left_normal(tangent)
    return QgsPointXY(point.x() + nx * distance, point.y() + ny * distance)


def _turn_angle_deg(v1, v2):
    n1 = hypot(v1[0], v1[1])
    n2 = hypot(v2[0], v2[1])
    if n1 <= 1e-12 or n2 <= 1e-12:
        return 0.0
    dot = max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)))
    return degrees(acos(dot))


def _transform_point(point, source_crs, target_crs):
    point = QgsPointXY(point)
    if source_crs == target_crs:
        return point
    return QgsCoordinateTransform(source_crs, target_crs, QgsProject.instance().transformContext()).transform(point)


def _transform_points(points, source_crs, target_crs):
    return [_transform_point(p, source_crs, target_crs) for p in points]


def _geometry_points(points):
    return [QgsPointXY(float(p[0]), float(p[1])) for p in points if len(p) >= 2]


def _route_edges_nodes(segment, work_crs, source_crs, edge_crs):
    if not segment:
        return [], []
    try:
        return _v5._v3._edge_nodes_for_segment(segment, work_crs, source_crs, edge_crs)
    except Exception as exc:
        _log(f"[fat-route-node-error] {type(exc).__name__}: {exc}", Qgis.Warning)
        return [], []


def _estimated_slot_from_segment(segment, edge_index, spacing, work_crs, source_crs, edge_crs):
    edges, nodes = _route_edges_nodes(segment, work_crs, source_crs, edge_crs)
    points = _geometry_points(segment.get("points", []) or [])
    if edge_index < 0 or edge_index >= len(edges) or len(nodes) != len(edges) + 1 or len(points) < 2:
        return None
    a, b = nodes[edge_index], nodes[edge_index + 1]
    edge_len = hypot(b.x() - a.x(), b.y() - a.y())
    if edge_len <= 1e-9:
        return None
    work_points = _transform_points(points, source_crs, work_crs)
    geom = QgsGeometry.fromPolylineXY(work_points)
    midpoint = QgsPointXY((a.x() + b.x()) * 0.5, (a.y() + b.y()) * 0.5)
    nearest = geom.nearestPoint(QgsGeometry.fromPointXY(midpoint))
    if nearest.isEmpty():
        return None
    p = nearest.asPoint()
    tx, ty = _unit(a, b)
    signed = (p.x() - midpoint.x()) * (-ty) + (p.y() - midpoint.y()) * tx
    if abs(signed) < max(spacing * 0.25, 0.01):
        return 0.0
    return float(round(signed / spacing) * spacing)


def _classify_corner(in_nodes, out_nodes, threshold_deg):
    if len(in_nodes) < 2 or len(out_nodes) < 2:
        return False, 0.0
    vin = _unit(in_nodes[-2], in_nodes[-1])
    vout = _unit(out_nodes[0], out_nodes[1])
    angle = _turn_angle_deg(vin, vout)
    # 180 degrees means the route is continuing straight through the graph
    # with reversed edge orientation, not making a physical corner.
    return angle >= threshold_deg and angle < 175.0, angle


def _classify_endpoint_corner(nodes, at_start, threshold_deg):
    if len(nodes) < 3:
        return False, 0.0
    if at_start:
        v1 = _unit(nodes[0], nodes[1])
        v2 = _unit(nodes[1], nodes[2])
    else:
        v1 = _unit(nodes[-3], nodes[-2])
        v2 = _unit(nodes[-2], nodes[-1])
    angle = _turn_angle_deg(v1, v2)
    return angle >= threshold_deg and angle < 175.0, angle


def _control_line_point(anchor, tangent, signed_offset, control_distance):
    center = QgsPointXY(anchor.x() + tangent[0] * control_distance, anchor.y() + tangent[1] * control_distance)
    return _offset_point(center, tangent, signed_offset)


def _find_generated_bend(points, anchor, control_distance):
    """Find an actual bend on the fixed control-distance line band."""
    if len(points) < 3:
        return None
    tolerance = max(0.05, control_distance * 0.35)
    candidates = []
    for i in range(1, len(points) - 1):
        p = points[i]
        radial = hypot(p.x() - anchor.x(), p.y() - anchor.y())
        if abs(radial - control_distance) > tolerance:
            continue
        vin = (p.x() - points[i - 1].x(), p.y() - points[i - 1].y())
        vout = (points[i + 1].x() - p.x(), points[i + 1].y() - p.y())
        turn = _turn_angle_deg(vin, vout)
        if turn < 20.0 or turn >= 175.0:
            continue
        candidates.append((abs(radial - control_distance), -turn, QgsPointXY(p), turn))
    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[0], x[1]))
    return candidates[0][2], candidates[0][3]


def _feature_point_in_work(feature, layer, work_crs):
    try:
        return _transform_point(QgsPointXY(feature.geometry().centroid().asPoint()), layer.crs(), work_crs)
    except Exception:
        return None


def _target_for_fat(ref, design, fat_feature, fat_layer, work_crs, edge_crs, spacing, control_distance, corner_threshold_deg):
    segments = design.get("segments", []) or []
    pos = int(ref["sequence_pos"])
    incoming_index = pos - 1 if 0 <= pos - 1 < len(segments) else None
    outgoing_index = pos if 0 <= pos < len(segments) else None
    incoming = segments[incoming_index] if incoming_index is not None else None
    outgoing = segments[outgoing_index] if outgoing_index is not None else None
    in_edges, in_nodes = _route_edges_nodes(incoming, work_crs, edge_crs, edge_crs)
    out_edges, out_nodes = _route_edges_nodes(outgoing, work_crs, edge_crs, edge_crs)

    anchor = QgsPointXY(in_nodes[-1]) if in_nodes else (QgsPointXY(out_nodes[0]) if out_nodes else _feature_point_in_work(fat_feature, fat_layer, work_crs))
    if anchor is None:
        return None, {"reason": "无法确定 FAT 所属 Pole Edge 节点"}

    if in_nodes and out_nodes:
        is_corner, turn = _classify_corner(in_nodes, out_nodes, corner_threshold_deg)
    elif out_nodes:
        is_corner, turn = _classify_endpoint_corner(out_nodes, True, corner_threshold_deg)
    elif in_nodes:
        is_corner, turn = _classify_endpoint_corner(in_nodes, False, corner_threshold_deg)
    else:
        is_corner, turn = False, 0.0

    if not is_corner:
        candidates = []
        if incoming is not None and len(in_nodes) >= 2:
            signed = _estimated_slot_from_segment(incoming, len(in_edges) - 1, spacing, work_crs, edge_crs, edge_crs)
            if signed is not None:
                candidates.append((abs(signed), signed, _unit(in_nodes[-2], in_nodes[-1]), "incoming"))
        if outgoing is not None and len(out_nodes) >= 2:
            signed = _estimated_slot_from_segment(outgoing, 0, spacing, work_crs, edge_crs, edge_crs)
            if signed is not None:
                candidates.append((abs(signed), signed, _unit(out_nodes[0], out_nodes[1]), "outgoing"))
        if not candidates:
            return None, {"reason": "无法从偏移后的 Cable 几何确定 FAT 对应槽位"}
        candidates.sort(key=lambda item: item[0], reverse=True)
        _, signed, tangent, side = candidates[0]
        target = _offset_point(anchor, tangent, signed) if abs(signed) > max(spacing * 0.25, 0.01) else QgsPointXY(anchor)
        return target, {"mode": "straight", "turn_angle": turn, "offset": signed, "anchor": anchor, "side": side}

    local = []
    for seg in (incoming, outgoing):
        if seg:
            local.extend(_transform_points(_geometry_points(seg.get("points", []) or []), edge_crs, work_crs))
    generated = _find_generated_bend(local, anchor, control_distance)
    if generated is not None:
        p, bend_turn = generated
        return p, {"mode": "corner_generated", "turn_angle": bend_turn, "offset": None, "anchor": anchor, "control_distance": control_distance}

    signed = None
    tangent = None
    if incoming is not None and len(in_nodes) >= 2:
        signed = _estimated_slot_from_segment(incoming, len(in_edges) - 1, spacing, work_crs, edge_crs, edge_crs)
        tangent = _unit(in_nodes[-2], in_nodes[-1])
    if (signed is None or abs(signed) <= max(spacing * 0.25, 0.01)) and outgoing is not None and len(out_nodes) >= 2:
        candidate = _estimated_slot_from_segment(outgoing, 0, spacing, work_crs, edge_crs, edge_crs)
        if candidate is not None:
            signed = candidate
            tangent = _unit(out_nodes[0], out_nodes[1])
    if signed is None or tangent is None or abs(signed) <= max(spacing * 0.25, 0.01):
        return None, {"reason": "拐角处无法确定非零偏移方向"}
    target = _control_line_point(anchor, tangent, signed, control_distance)
    return target, {"mode": "corner_control", "turn_angle": turn, "offset": signed, "anchor": anchor, "control_distance": control_distance}


def _make_fat_index(fat_layer):
    return {int(feature.id()): feature for feature in fat_layer.getFeatures()} if fat_layer is not None else {}


def _fat_sequence_refs(designs):
    refs, duplicates = {}, set()
    for di, design in enumerate(designs or []):
        for pos, item in enumerate(design.get("sequence_ids", []) or []):
            if len(item) < 2 or str(item[0]).upper() != "FAT":
                continue
            try:
                fid = int(item[1])
            except Exception:
                continue
            if fid in refs:
                duplicates.add(fid)
                continue
            refs[fid] = {"design_index": di, "sequence_pos": pos}
    return refs, duplicates


def _find_segment_for_sequence_pos(pos, count):
    return (pos - 1 if pos > 0 and pos - 1 < count else None, pos if pos < count else None)


def _replace_fat_endpoints(designs, moves, edge_crs):
    changed = 0
    for move in moves.values():
        di, pos = move["design_index"], move["sequence_pos"]
        if di < 0 or di >= len(designs):
            continue
        design = designs[di]
        source_crs = _crs_from_authid(design.get("source_crs")) or edge_crs
        target = _transform_point(move["target_edge"], edge_crs, source_crs)
        incoming, outgoing = _find_segment_for_sequence_pos(pos, len(design.get("segments", []) or []))
        touched = False
        if incoming is not None:
            segment = design["segments"][incoming]
            pts = list(segment.get("points", []) or [])
            if pts:
                if len(pts) == 1:
                    # FDT and FAT originally occupy the same coordinate.  The
                    # FAT landing point is the authoritative new endpoint, so
                    # turn the logical one-point segment into a real FDT -> FAT
                    # line instead of allowing it to reach the DC writer as an
                    # invalid/empty geometry.
                    pts.append(list(pts[0]))
                    _log(
                        f"[fat-landing-zero-segment] design={di}; sequence_pos={pos}; "
                        "FDT/FAT initially coincident; expanded segment before writing",
                        Qgis.Info,
                    )
                pts[-1] = [float(target.x()), float(target.y())]
                segment["points"] = pts
                segment["distance"] = _polyline_distance(pts, source_crs)
                segment["zero_length"] = False
                touched = True
            else:
                anchor = move.get("target_edge")
                if anchor is not None:
                    anchor_src = _transform_point(anchor, edge_crs, source_crs)
                    segment["points"] = [
                        [float(anchor_src.x()), float(anchor_src.y())],
                        [float(target.x()), float(target.y())],
                    ]
                    segment["distance"] = _polyline_distance(segment["points"], source_crs)
                    segment["zero_length"] = False
                    touched = True
        if outgoing is not None:
            pts = list(design["segments"][outgoing].get("points", []) or [])
            if pts:
                if len(pts) == 1:
                    pts.insert(0, list(pts[0]))
                pts[0] = [float(target.x()), float(target.y())]
                design["segments"][outgoing]["points"] = pts
                design["segments"][outgoing]["distance"] = _polyline_distance(pts, source_crs)
                design["segments"][outgoing]["zero_length"] = False
                touched = True
        changed += int(touched)
    return changed


def _polyline_distance(points, crs):
    if len(points) < 2:
        return 0.0
    try:
        if crs.isGeographic():
            return 0.0
        total = 0.0
        for i in range(1, len(points)):
            total += hypot(
                float(points[i][0]) - float(points[i - 1][0]),
                float(points[i][1]) - float(points[i - 1][1]),
            )
        return round(total, 3)
    except Exception:
        total = 0.0
        for i in range(1, len(points)):
            total += hypot(
                float(points[i][0]) - float(points[i - 1][0]),
                float(points[i][1]) - float(points[i - 1][1]),
            )
        return round(total, 3)


def _apply_fat_moves(fat_layer, moves):
    prepared = []
    for fid, move in moves.items():
        feature = fat_layer.getFeature(int(fid))
        if not feature or not feature.isValid():
            raise RuntimeError(f"FAT feature {fid} 不存在，无法更新 FAT 落点。")
        target = move.get("target_layer")
        if target is None:
            raise RuntimeError(f"FAT feature {fid} 的目标坐标无效。")
        prepared.append((feature, target))
    moved = 0
    for feature, target in prepared:
        new_geom = QgsGeometry.fromPointXY(QgsPointXY(target))
        if feature.geometry().distance(new_geom) > 1e-9:
            feature.setGeometry(new_geom)
            if not fat_layer.updateFeature(feature):
                raise RuntimeError(f"无法写入 FAT feature {feature.id()} 的新落点。")
            moved += 1
    return moved


def prepare_fat_landing_points(designs, fat_layer, edge_layer, spacing, control_distance_m, fat_max_distance_m=DEFAULT_FAT_MAX_DISTANCE_M, corner_threshold_deg=DEFAULT_CORNER_THRESHOLD_DEG):
    if fat_layer is None or edge_layer is None:
        return {}, {"total": 0, "skipped": 0, "corner": 0, "straight": 0}
    edge_crs = edge_layer.crs()
    work_crs = _v5._v3._choose_metric_work_crs(edge_layer, fat_layer)
    if work_crs.isGeographic():
        raise RuntimeError(f"FAT 落点计算需要米制工作 CRS，但当前为 {work_crs.authid()}。")
    fat_features = _make_fat_index(fat_layer)
    refs, duplicates = _fat_sequence_refs(designs)
    if duplicates:
        _log(f"[fat-landing] duplicate_fat_refs={sorted(duplicates)[:20]}", Qgis.Warning)
    moves, stats = {}, {"total": len(refs), "skipped": 0, "corner": 0, "straight": 0}
    for fid, ref in refs.items():
        feature = fat_features.get(fid)
        if feature is None:
            stats["skipped"] += 1
            _log(f"[fat-landing-skip] fid={fid}; reason=FAT图层找不到feature", Qgis.Warning)
            continue
        current = _feature_point_in_work(feature, fat_layer, work_crs)
        if current is None:
            stats["skipped"] += 1
            _log(f"[fat-landing-skip] fid={fid}; reason=无法读取FAT点位置", Qgis.Warning)
            continue
        target_work, info = _target_for_fat(ref, designs[ref["design_index"]], feature, fat_layer, work_crs, edge_crs, float(spacing), float(control_distance_m), float(corner_threshold_deg))
        if target_work is None:
            stats["skipped"] += 1
            _log(f"[fat-landing-skip] fid={fid}; design={ref['design_index']}; reason={info.get('reason', 'unknown')}", Qgis.Warning)
            continue
        anchor = info.get("anchor")
        if anchor is not None:
            from_anchor = hypot(current.x() - anchor.x(), current.y() - anchor.y())
            if from_anchor > max(float(fat_max_distance_m), 0.01):
                stats["skipped"] += 1
                _log(f"[fat-landing-skip] fid={fid}; distance_to_pole={from_anchor:.3f}m; limit={float(fat_max_distance_m):.3f}m", Qgis.Warning)
                continue
        target_edge = _transform_point(target_work, work_crs, edge_crs)
        target_layer = _transform_point(target_edge, edge_crs, fat_layer.crs())
        move_distance = hypot(current.x() - target_work.x(), current.y() - target_work.y())
        moves[fid] = {"design_index": ref["design_index"], "sequence_pos": ref["sequence_pos"], "target_edge": target_edge, "target_layer": target_layer, "current_work": current, "target_work": target_work, "mode": info.get("mode", "unknown"), "turn_angle": float(info.get("turn_angle", 0.0) or 0.0), "offset": info.get("offset"), "anchor": anchor, "move_distance": move_distance}
        if str(info.get("mode", "")).startswith("corner"):
            stats["corner"] += 1
        else:
            stats["straight"] += 1
        _log(f"[fat-landing] fid={fid}; design={ref['design_index']}; seq_pos={ref['sequence_pos']}; mode={info.get('mode')}; turn={float(info.get('turn_angle', 0.0) or 0.0):.2f}deg; offset={(float(info['offset']) if info.get('offset') is not None else 0.0):+.3f}m; move={move_distance:.3f}m")
    return moves, stats


def apply_explicit_layout_to_designs(designs, distribution_layer, edge_layer, spacing=DEFAULT_SPACING_M, control_distance_m=DEFAULT_CONTROL_DISTANCE_M, fat_layer=None, fat_max_distance_m=DEFAULT_FAT_MAX_DISTANCE_M, corner_threshold_deg=DEFAULT_CORNER_THRESHOLD_DEG):
    summary = dict(_v5.apply_explicit_layout_to_designs(designs, distribution_layer, edge_layer, spacing=spacing, control_distance_m=control_distance_m) or {})
    moves, stats = prepare_fat_landing_points(designs, fat_layer, edge_layer, spacing, control_distance_m, fat_max_distance_m=fat_max_distance_m, corner_threshold_deg=corner_threshold_deg)
    endpoint_changes = _replace_fat_endpoints(designs, moves, edge_layer.crs()) if moves else 0
    summary.update({"fat_moves": moves, "fat_total": stats["total"], "fat_skipped": stats["skipped"], "fat_corner": stats["corner"], "fat_straight": stats["straight"], "fat_endpoint_updates": endpoint_changes, "fat_corner_threshold_deg": float(corner_threshold_deg), "fat_max_distance_m": float(fat_max_distance_m)})
    _log(f"[fat-result] total={stats['total']}; moved={len(moves)}; straight={stats['straight']}; corner={stats['corner']}; skipped={stats['skipped']}; endpoint_updates={endpoint_changes}")
    return summary


def commit_fat_landing_points(fat_layer, summary):
    moves = (summary or {}).get("fat_moves") or {}
    moved = _apply_fat_moves(fat_layer, moves)
    summary["fat_written"] = moved
    return moved
