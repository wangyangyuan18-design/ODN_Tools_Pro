# -*- coding: utf-8 -*-
"""Cable offset + FAT landing-point layout.

This layer keeps the validated v5 cable-offset engine and adds the physical
FAT landing rule used by Link Design:

1. FAT is first associated with the Pole Edge graph node (the POLE) to which
   the routing engine already attaches it. The same 3 m engineering rule is
   used by Link Design's route engine/configuration.
2. The FAT's owning Link, not the POLE, determines its final displayed point.
3. If the Link continues straight through that POLE, the FAT is placed on the
   Link's offset run at the configured lateral spacing.
4. If the Link makes a real corner at that POLE, the FAT is placed on the
   generated offset corner / control-distance transition point.
5. Cable segment endpoints are updated to the same FAT point so the written
   Distribution Cable remains topologically connected to the moved FAT.

No manual pole or FAT layer selection is introduced; project layer bindings
remain authoritative.
"""

from math import acos, degrees, hypot

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    Qgis,
)

from . import cable_offset_layout_v5 as _v5

DEFAULT_CORNER_THRESHOLD_DEG = 20.0
DEFAULT_FAT_MAX_DISTANCE_M = 3.0
LOG_TAG = "ODN_Tools_Pro / Cable Offset"


def _log(message, level=Qgis.Info):
    try:
        QgsMessageLog.logMessage(str(message), LOG_TAG, level)
    except Exception:
        pass


# QgsMessageLog is imported lazily above for older QGIS builds where the
# symbol may be unavailable through some minimal imports.
try:
    from qgis.core import QgsMessageLog
except Exception:  # pragma: no cover
    QgsMessageLog = None


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
    return QgsCoordinateTransform(
        source_crs, target_crs, QgsProject.instance().transformContext()
    ).transform(point)


def _transform_points(points, source_crs, target_crs):
    return [_transform_point(p, source_crs, target_crs) for p in points]


def _geometry_points(points):
    return [QgsPointXY(float(p[0]), float(p[1])) for p in points if len(p) >= 2]


def _route_edges_nodes(segment, work_crs, source_crs, edge_crs):
    try:
        edges, nodes = _v5._v3._edge_nodes_for_segment(
            segment, work_crs, source_crs, edge_crs
        )
        return edges, nodes
    except Exception:
        return [], []


def _estimate_signed_offset(segment, segment_points, edge_index, work_crs, source_crs, edge_crs, spacing):
    """Estimate the actual slot offset on one authoritative Pole Edge."""
    edges, nodes = _route_edges_nodes(segment, work_crs, source_crs, edge_crs)
    if edge_index < 0 or edge_index >= len(edges) or edge_index >= len(nodes) - 1:
        return 0.0
    a, b = nodes[edge_index], nodes[edge_index + 1]
    edge_len = hypot(b.x() - a.x(), b.y() - a.y())
    if edge_len <= 1e-9:
        return 0.0
    points = _transform_points(_geometry_points(segment_points), source_crs, work_crs)
    if len(points) < 2:
        return 0.0
    geom = QgsGeometry.fromPolylineXY(points)
    midpoint = QgsPointXY(
        a.x() + (b.x() - a.x()) * 0.5,
        a.y() + (b.y() - a.y()) * 0.5,
    )
    try:
        nearest = geom.nearestPoint(QgsGeometry.fromPointXY(midpoint))
        if nearest.isEmpty():
            return 0.0
        p = nearest.asPoint()
    except Exception:
        return 0.0
    tx, ty = _unit(a, b)
    signed = (p.x() - midpoint.x()) * (-ty) + (p.y() - midpoint.y()) * tx
    # Quantize to the actual configured corridor. This suppresses tiny
    # numerical/digitizing noise and makes the FAT point agree with the
    # selected cable slot (0, +/-spacing, +/-2*spacing, ...).
    if abs(signed) < max(spacing * 0.25, 0.01):
        return 0.0
    slot = round(signed / spacing)
    return float(slot) * spacing


def _flatten_local_points(incoming, outgoing, anchor, work_crs, source_crs):
    points = []
    for raw in (incoming or []):
        try:
            p = _transform_point(QgsPointXY(float(raw[0]), float(raw[1])), source_crs, work_crs)
        except Exception:
            continue
        if not points or hypot(points[-1].x() - p.x(), points[-1].y() - p.y()) > 1e-8:
            points.append(p)
    for raw in (outgoing or []):
        try:
            p = _transform_point(QgsPointXY(float(raw[0]), float(raw[1])), source_crs, work_crs)
        except Exception:
            continue
        if not points or hypot(points[-1].x() - p.x(), points[-1].y() - p.y()) > 1e-8:
            points.append(p)
    return points


def _find_corner_candidate(points, anchor, control_distance, max_radius):
    """Choose the generated offset bend closest to the control line distance."""
    if len(points) < 3:
        return None
    candidates = []
    for i in range(1, len(points) - 1):
        before = points[i - 1]
        here = points[i]
        after = points[i + 1]
        dist = hypot(here.x() - anchor.x(), here.y() - anchor.y())
        if dist < 0.02 or dist > max_radius:
            continue
        v1 = (here.x() - before.x(), here.y() - before.y())
        v2 = (after.x() - here.x(), after.y() - here.y())
        turn = _turn_angle_deg(v1, v2)
        if turn < 5.0:
            continue
        # The first criterion enforces the control-distance concept; the
        # second prefers the actual geometric bend over an almost-straight
        # transition vertex when several candidates are similarly placed.
        score = (abs(dist - control_distance), -turn, dist)
        candidates.append((score, here, turn, dist))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def _classify_corner(incoming_nodes, outgoing_nodes, threshold_deg):
    if incoming_nodes is None or outgoing_nodes is None:
        return False, 0.0
    if len(incoming_nodes) < 2 or len(outgoing_nodes) < 2:
        return False, 0.0
    vin = _unit(incoming_nodes[-2], incoming_nodes[-1])
    vout = _unit(outgoing_nodes[0], outgoing_nodes[1])
    angle = _turn_angle_deg(vin, vout)
    return angle >= threshold_deg, angle


def _feature_point_in_work(feature, layer, work_crs):
    try:
        point = feature.geometry().centroid().asPoint()
        return _transform_point(QgsPointXY(point), layer.crs(), work_crs)
    except Exception:
        return None


def _make_fat_index(fat_layer):
    result = {}
    if fat_layer is None:
        return result
    for feature in fat_layer.getFeatures():
        result[int(feature.id())] = feature
    return result


def _fat_sequence_refs(designs):
    refs = {}
    duplicates = set()
    for di, design in enumerate(designs or []):
        sequence = design.get("sequence_ids", []) or []
        for pos, item in enumerate(sequence):
            if len(item) < 2 or str(item[0]).upper() != "FAT":
                continue
            try:
                fid = int(item[1])
            except Exception:
                continue
            if fid in refs:
                duplicates.add(fid)
            refs[fid] = {
                "design_index": di,
                "sequence_pos": pos,
                "sequence": sequence,
            }
    return refs, duplicates


def _segment_points_map(design):
    return {
        index: list(segment.get("points", []) or [])
        for index, segment in enumerate(design.get("segments", []) or [])
    }


def _find_segment_for_sequence_pos(pos, segment_count):
    # Segment i connects sequence[i] -> sequence[i+1]. A FAT at sequence
    # position p therefore has incoming segment p-1 and outgoing segment p.
    incoming = pos - 1 if pos > 0 and pos - 1 < segment_count else None
    outgoing = pos if pos < segment_count else None
    return incoming, outgoing


def _target_for_fat(
    fat_id,
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
    segments = design.get("segments", []) or []
    sequence_pos = int(ref["sequence_pos"])
    incoming_index, outgoing_index = _find_segment_for_sequence_pos(
        sequence_pos, len(segments)
    )
    incoming_segment = segments[incoming_index] if incoming_index is not None else None
    outgoing_segment = segments[outgoing_index] if outgoing_index is not None else None

    in_edges, in_nodes = _route_edges_nodes(
        incoming_segment, work_crs, edge_crs, edge_crs
    ) if incoming_segment else ([], [])
    out_edges, out_nodes = _route_edges_nodes(
        outgoing_segment, work_crs, edge_crs, edge_crs
    ) if outgoing_segment else ([], [])

    anchor = None
    if in_nodes:
        anchor = QgsPointXY(in_nodes[-1])
    elif out_nodes:
        anchor = QgsPointXY(out_nodes[0])
    else:
        anchor = _feature_point_in_work(fat_feature, fat_layer, work_crs)
    if anchor is None:
        return None, {"reason": "无法确定 FAT 所属 Pole Edge 节点"}

    is_corner, turn_angle = _classify_corner(in_nodes, out_nodes, corner_threshold_deg)

    incoming_points = list(incoming_segment.get("points", []) or []) if incoming_segment else []
    outgoing_points = list(outgoing_segment.get("points", []) or []) if outgoing_segment else []

    if not is_corner:
        candidates = []
        if in_edges and in_nodes:
            signed = _estimate_signed_offset(
                incoming_segment,
                incoming_points,
                len(in_edges) - 1,
                work_crs,
                edge_crs,
                edge_crs,
                spacing,
            )
            candidates.append((abs(signed), signed, _unit(in_nodes[-2], in_nodes[-1])))
        if out_edges and out_nodes and len(out_nodes) >= 2:
            signed = _estimate_signed_offset(
                outgoing_segment,
                outgoing_points,
                0,
                work_crs,
                edge_crs,
                edge_crs,
                spacing,
            )
            candidates.append((abs(signed), signed, _unit(out_nodes[0], out_nodes[1])))
        if not candidates:
            return None, {"reason": "无法估计 FAT 对应 Link 的偏移槽位"}
        candidates.sort(key=lambda item: item[0], reverse=True)
        _, signed, tangent = candidates[0]
        target_work = _offset_point(anchor, tangent, signed)
        mode = "straight"
        if abs(signed) <= max(spacing * 0.25, 0.01):
            target_work = QgsPointXY(anchor)
        return target_work, {
            "mode": mode,
            "turn_angle": turn_angle,
            "offset": signed,
            "anchor": anchor,
            "incoming": incoming_index,
            "outgoing": outgoing_index,
        }

    # Large corner: prefer the actual generated offset bend that lies close
    # to the fixed control-distance line. This makes the FAT a real cable bend
    # point instead of leaving it at the POLE where L1 may visually capture it.
    local_points = _flatten_local_points(
        incoming_points,
        outgoing_points,
        anchor,
        work_crs,
        edge_crs,
    )
    max_radius = max(1.0, control_distance * 4.0, spacing * 5.0)
    candidate = _find_corner_candidate(
        local_points, anchor, control_distance, max_radius
    )
    if candidate is not None:
        return QgsPointXY(candidate), {
            "mode": "corner_generated",
            "turn_angle": turn_angle,
            "offset": None,
            "anchor": anchor,
            "incoming": incoming_index,
            "outgoing": outgoing_index,
            "control_distance": control_distance,
        }

    # Safe geometric fallback: use the incoming control line if available,
    # otherwise the outgoing one, while preserving the estimated cable side.
    fallback_info = None
    if in_edges and in_nodes:
        signed = _estimate_signed_offset(
            incoming_segment,
            incoming_points,
            len(in_edges) - 1,
            work_crs,
            edge_crs,
            edge_crs,
            spacing,
        )
        tangent = _unit(in_nodes[-2], in_nodes[-1])
        fallback_info = (signed, tangent)
    elif out_edges and out_nodes and len(out_nodes) >= 2:
        signed = _estimate_signed_offset(
            outgoing_segment,
            outgoing_points,
            0,
            work_crs,
            edge_crs,
            edge_crs,
            spacing,
        )
        tangent = _unit(out_nodes[0], out_nodes[1])
        fallback_info = (signed, tangent)
    if fallback_info is None:
        return None, {"reason": "拐角附近未找到有效 Cable 拐点"}
    signed, tangent = fallback_info
    control_point = QgsPointXY(
        anchor.x() + tangent[0] * control_distance,
        anchor.y() + tangent[1] * control_distance,
    )
    target_work = _offset_point(control_point, tangent, signed)
    return target_work, {
        "mode": "corner_control_fallback",
        "turn_angle": turn_angle,
        "offset": signed,
        "anchor": anchor,
        "incoming": incoming_index,
        "outgoing": outgoing_index,
        "control_distance": control_distance,
    }


def _replace_fat_endpoints(designs, moves, edge_crs):
    """Make copied Link geometries terminate/start at the moved FAT point."""
    changed = 0
    for fid, move in moves.items():
        design_index = move["design_index"]
        sequence_pos = move["sequence_pos"]
        if design_index < 0 or design_index >= len(designs):
            continue
        design = designs[design_index]
        target_edge = move["target_edge"]
        source_crs = _v5._v3._crs_from_authid(design.get("source_crs"))
        if source_crs is None:
            source_crs = edge_crs
        target = _transform_point(target_edge, edge_crs, source_crs)
        segments = design.get("segments", []) or []
        incoming_index, outgoing_index = _find_segment_for_sequence_pos(
            sequence_pos, len(segments)
        )
        touched = False
        if incoming_index is not None and incoming_index < len(segments):
            points = list(segments[incoming_index].get("points", []) or [])
            if points:
                points[-1] = [float(target.x()), float(target.y())]
                segments[incoming_index]["points"] = points
                touched = True
        if outgoing_index is not None and outgoing_index < len(segments):
            points = list(segments[outgoing_index].get("points", []) or [])
            if points:
                points[0] = [float(target.x()), float(target.y())]
                segments[outgoing_index]["points"] = points
                touched = True
        if touched:
            changed += 1

    for design in designs or []:
        total = 0.0
        for segment in design.get("segments", []) or []:
            points = _geometry_points(segment.get("points", []) or [])
            if len(points) < 2:
                continue
            try:
                source_crs = _v5._v3._crs_from_authid(design.get("source_crs")) or edge_crs
                work_points = _transform_points(points, source_crs, edge_crs)
                total += QgsGeometry.fromPolylineXY(work_points).length()
            except Exception:
                pass
        if total > 0:
            design["length"] = round(float(total), 3)
    return changed


def _apply_fat_moves(fat_layer, moves):
    if fat_layer is None or not moves:
        return 0
    moved = 0
    for fid, move in moves.items():
        feature = fat_layer.getFeature(int(fid))
        if not feature or not feature.isValid():
            raise RuntimeError(f"FAT feature {fid} 不存在，无法更新 FAT 落点。")
        target = move["target_layer"]
        geom = feature.geometry()
        if geom is None or geom.isEmpty():
            raise RuntimeError(f"FAT feature {fid} 几何为空，无法更新 FAT 落点。")
        new_geom = QgsGeometry.fromPointXY(QgsPointXY(target))
        if geom != new_geom:
            feature.setGeometry(new_geom)
            if not fat_layer.updateFeature(feature):
                raise RuntimeError(f"无法写入 FAT feature {fid} 的新落点。")
            moved += 1
    return moved


def prepare_fat_landing_points(
    designs,
    fat_layer,
    edge_layer,
    spacing,
    control_distance_m,
    fat_max_distance_m=DEFAULT_FAT_MAX_DISTANCE_M,
    corner_threshold_deg=DEFAULT_CORNER_THRESHOLD_DEG,
):
    """Calculate FAT landing points without mutating the FAT layer."""
    if fat_layer is None or edge_layer is None:
        return {}, {"total": 0, "skipped": 0, "corner": 0, "straight": 0}

    edge_crs = edge_layer.crs()
    work_crs = _v5._v3._choose_metric_work_crs(edge_layer, fat_layer)
    if work_crs.isGeographic():
        raise RuntimeError(f"FAT 落点计算需要米制工作 CRS，但当前为 {work_crs.authid()}。")

    fat_features = _make_fat_index(fat_layer)
    refs, duplicates = _fat_sequence_refs(designs)
    if duplicates:
        _log(
            f"[fat-landing] duplicate_fat_refs={sorted(duplicates)[:20]}",
            Qgis.Warning,
        )

    moves = {}
    stats = {"total": len(refs), "skipped": 0, "corner": 0, "straight": 0}
    for fid, ref in refs.items():
        feature = fat_features.get(fid)
        if feature is None:
            stats["skipped"] += 1
            _log(
                f"[fat-landing-skip] fid={fid}; reason=FAT图层找不到feature",
                Qgis.Warning,
            )
            continue
        point = _feature_point_in_work(feature, fat_layer, work_crs)
        if point is None:
            stats["skipped"] += 1
            continue
        design = designs[ref["design_index"]]
        target_work, info = _target_for_fat(
            fid,
            ref,
            design,
            feature,
            fat_layer,
            work_crs,
            edge_crs,
            float(spacing),
            float(control_distance_m),
            float(corner_threshold_deg),
        )
        if target_work is None:
            stats["skipped"] += 1
            _log(
                f"[fat-landing-skip] fid={fid}; design={ref['design_index']}; "
                f"reason={info.get('reason', 'unknown')}",
                Qgis.Warning,
            )
            continue

        current_distance = hypot(point.x() - target_work.x(), point.y() - target_work.y())
        # FAT must already be near a POLE according to the project's physical
        # attachment rule. Refuse to silently relocate a remote FAT.
        anchor = info.get("anchor")
        if anchor is not None:
            from_anchor = hypot(point.x() - anchor.x(), point.y() - anchor.y())
            if from_anchor > max(float(fat_max_distance_m), 0.01):
                stats["skipped"] += 1
                _log(
                    f"[fat-landing-skip] fid={fid}; distance_to_pole={from_anchor:.3f}m; "
                    f"limit={float(fat_max_distance_m):.3f}m",
                    Qgis.Warning,
                )
                continue

        target_edge = _transform_point(target_work, work_crs, edge_crs)
        target_layer = _transform_point(target_edge, edge_crs, fat_layer.crs())
        moves[fid] = {
            "design_index": ref["design_index"],
            "sequence_pos": ref["sequence_pos"],
            "target_edge": target_edge,
            "target_layer": target_layer,
            "current_work": point,
            "target_work": target_work,
            "mode": info.get("mode", "unknown"),
            "turn_angle": float(info.get("turn_angle", 0.0) or 0.0),
            "offset": info.get("offset"),
            "anchor": anchor,
            "move_distance": current_distance,
        }
        if str(info.get("mode", "")).startswith("corner"):
            stats["corner"] += 1
        else:
            stats["straight"] += 1
        _log(
            f"[fat-landing] fid={fid}; design={ref['design_index']}; seq_pos={ref['sequence_pos']}; "
            f"mode={info.get('mode')}; turn={float(info.get('turn_angle', 0.0) or 0.0):.2f}deg; "
            f"offset={(float(info['offset']) if info.get('offset') is not None else 0.0):+.3f}m; "
            f"move={current_distance:.3f}m"
        )

    return moves, stats


def apply_explicit_layout_to_designs(
    designs,
    distribution_layer,
    edge_layer,
    spacing=_v5.DEFAULT_SPACING_M,
    control_distance_m=_v5.DEFAULT_CONTROL_DISTANCE_M,
    fat_layer=None,
    fat_max_distance_m=DEFAULT_FAT_MAX_DISTANCE_M,
    corner_threshold_deg=DEFAULT_CORNER_THRESHOLD_DEG,
):
    """Apply cable offset and prepare FAT relocation in one result."""
    summary = _v5.apply_explicit_layout_to_designs(
        designs,
        distribution_layer,
        edge_layer,
        spacing=spacing,
        control_distance_m=control_distance_m,
    )
    summary = dict(summary or {})

    moves, fat_stats = prepare_fat_landing_points(
        designs,
        fat_layer,
        edge_layer,
        spacing,
        control_distance_m,
        fat_max_distance_m=fat_max_distance_m,
        corner_threshold_deg=corner_threshold_deg,
    )

    endpoint_changes = _replace_fat_endpoints(designs, moves, edge_layer.crs()) if moves else 0
    summary["fat_moves"] = moves
    summary["fat_total"] = fat_stats["total"]
    summary["fat_skipped"] = fat_stats["skipped"]
    summary["fat_corner"] = fat_stats["corner"]
    summary["fat_straight"] = fat_stats["straight"]
    summary["fat_endpoint_updates"] = endpoint_changes
    summary["fat_corner_threshold_deg"] = float(corner_threshold_deg)
    summary["fat_max_distance_m"] = float(fat_max_distance_m)
    _log(
        f"[fat-result] total={fat_stats['total']}; moved={len(moves)}; "
        f"straight={fat_stats['straight']}; corner={fat_stats['corner']}; "
        f"skipped={fat_stats['skipped']}; endpoint_updates={endpoint_changes}"
    )
    return summary


def commit_fat_landing_points(fat_layer, summary):
    """Write the previously prepared FAT landing points to the bound layer."""
    moves = (summary or {}).get("fat_moves") or {}
    moved = _apply_fat_moves(fat_layer, moves)
    summary["fat_written"] = moved
    return moved
