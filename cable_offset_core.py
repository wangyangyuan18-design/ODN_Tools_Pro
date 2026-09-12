# -*- coding: utf-8 -*-
"""Authoritative ODN cable offset engine.

This is the ONLY offset planning/geometry implementation.  It deliberately
contains no versioned v2/v3/v5/v6/v8/v9/v10 runtime chain and performs no
monkey-patching.  Link topology remains authoritative; this module only
creates output geometry for Distribution Cable and final FAT landing points.
"""
from math import acos, degrees, hypot, tan, radians

from qgis.PyQt.QtCore import QSettings
from qgis.core import (
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsDistanceArea,
    QgsFeature, QgsGeometry, QgsMessageLog, QgsPointXY, QgsProject,
    QgsRectangle, QgsSpatialIndex, QgsUnitTypes, Qgis,
)

from . import cable_offset_layout as _base

LOG_TAG = "ODN_Tools_Pro / Cable Offset"
DEFAULT_SPACING_M = 0.50
DEFAULT_CONTROL_M = 0.30
DEFAULT_CORNER_THRESHOLD_DEG = 20.0
DEFAULT_FAT_MAX_DISTANCE_M = 3.0
SPACING_KEY = "ODNToolsPro/CableOffsetLayout/spacing_m"
CONTROL_DISTANCE_KEY = "ODNToolsPro/CableOffsetLayout/control_distance_m"
SPECIAL = {"FDT", "BB", "CL", "CLOSURE", "SFCCL", "SFCCLOSURE"}


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
        control = float(QSettings().value(CONTROL_DISTANCE_KEY, DEFAULT_CONTROL_M))
    except Exception:
        control = DEFAULT_CONTROL_M
    return max(0.01, spacing), max(0.01, control)


def save_settings(spacing, control_distance):
    settings = QSettings()
    settings.setValue(SPACING_KEY, max(0.01, float(spacing)))
    settings.setValue(CONTROL_DISTANCE_KEY, max(0.01, float(control_distance)))
    settings.sync()


def _kind(item):
    if not item:
        return ""
    return "".join(ch for ch in str(item[0]).strip().upper() if ch.isalnum())


def _special(item):
    kind = _kind(item)
    return kind in SPECIAL or (kind.startswith("SFC") and ("CL" in kind or "CLOSURE" in kind))


def _metric_crs(edge_layer, dc_layer=None):
    for layer in (dc_layer, edge_layer):
        if layer is None:
            continue
        crs = layer.crs()
        try:
            if crs.isValid() and not crs.isGeographic() and crs.mapUnits() == QgsUnitTypes.DistanceMeters:
                return crs
        except Exception:
            pass
    crs = _base._choose_work_crs(edge_layer)
    if crs.isValid() and not crs.isGeographic() and crs.mapUnits() == QgsUnitTypes.DistanceMeters:
        return crs
    raise RuntimeError("Offset Core: 无法建立米制工作 CRS")


def _tp(point, source_crs, target_crs):
    return _base._transform_point(QgsPointXY(point), source_crs, target_crs)


def _node_key(point):
    return round(float(point.x()), 7), round(float(point.y()), 7)


def _turn_angle(a, b, c):
    v1 = _base._unit(a, b)
    v2 = _base._unit(b, c)
    dot = max(-1.0, min(1.0, v1[0] * v2[0] + v1[1] * v2[1]))
    return degrees(acos(dot))


def _route_metrics(design_index, design, edge_crs, work_crs):
    total = 0.0
    longest_run = 0.0
    current_run = 0.0
    turn_sum = 0.0
    turn_count = 0
    reversal_count = 0
    edge_count = 0
    seen = set()
    for segment in design.get("segments", []) or []:
        edges = [e for raw in segment.get("edge_sequence", []) or [] if (e := _base._canonical_edge(raw))]
        if not edges:
            continue
        nodes = _base._extract_route_graph_nodes(segment, work_crs, edge_crs, edge_crs)
        if len(nodes) != len(edges) + 1:
            continue
        for i, edge in enumerate(edges):
            a, b = _base._edge_points(edge)
            a, b = _tp(a, edge_crs, work_crs), _tp(b, edge_crs, work_crs)
            length = hypot(b.x() - a.x(), b.y() - a.y())
            edge_count += 1
            if edge not in seen:
                total += length
                seen.add(edge)
            current_run += length
            if i + 1 < len(edges):
                angle = _turn_angle(nodes[i], nodes[i + 1], nodes[i + 2])
                turn_count += 1
                turn_sum += angle
                if angle >= 135.0:
                    reversal_count += 1
                if angle >= 25.0:
                    longest_run = max(longest_run, current_run)
                    current_run = 0.0
    longest_run = max(longest_run, current_run)
    score = longest_run * 1000.0 + total * 10.0 - turn_count * 150.0 - reversal_count * 500.0
    return {
        "route_length": total,
        "longest_directional_run": longest_run,
        "turn_count": turn_count,
        "turn_sum": turn_sum,
        "reversal_count": reversal_count,
        "edge_count": edge_count,
        "priority_score": score,
    }


def _collect_uses(designs, edge_crs, work_crs):
    pending, routes = [], {}
    for design_index, design in enumerate(designs or []):
        if design.get("written") and not design.get("needs_resync"):
            continue
        metrics = _route_metrics(design_index, design, edge_crs, work_crs)
        routes[design_index] = metrics
        for segment_index, segment in enumerate(design.get("segments", []) or []):
            edges = [e for raw in segment.get("edge_sequence", []) or [] if (e := _base._canonical_edge(raw))]
            points = segment.get("points", []) or []
            if not edges or len(points) < 2:
                continue
            nodes = _base._extract_route_graph_nodes(segment, work_crs, edge_crs, edge_crs)
            if len(nodes) != len(edges) + 1:
                continue
            start = _tp(QgsPointXY(float(points[0][0]), float(points[0][1])), edge_crs, work_crs)
            end = _tp(QgsPointXY(float(points[-1][0]), float(points[-1][1])), edge_crs, work_crs)
            for edge_index, edge in enumerate(edges):
                a, b = _base._edge_points(edge)
                a, b = _tp(a, edge_crs, work_crs), _tp(b, edge_crs, work_crs)
                use = _base._LayoutUse(
                    design_index, segment_index, edge_index, edge, start, end,
                    _base._route_side_hint(start, end, a, b),
                )
                if edge_index > 0:
                    use.prev_edge_key = edges[edge_index - 1]
                pending.append(use)
    return pending, routes


def _occupancy(dc_layer, designs, work_crs):
    memory = QgsVectorLayer(f"LineString?crs={work_crs.authid()}", "ODN Offset Occupancy", "memory")
    field_index = dc_layer.fields().indexOf("_ODN_LINK_ID")
    current_ids = {str(d.get("_link_id")) for d in designs or [] if d.get("_link_id")}
    for feature in dc_layer.getFeatures():
        try:
            if field_index >= 0 and feature.attribute(field_index) and str(feature.attribute(field_index)) in current_ids:
                continue
            geom = _base._transform_geometry(feature.geometry(), dc_layer.crs(), work_crs)
            if not geom.isEmpty():
                out = QgsFeature()
                out.setGeometry(geom)
                memory.dataProvider().addFeature(out)
        except Exception:
            continue
    memory.updateExtents()
    return memory


def _node_users(designs, edge_crs, work_crs):
    users = {}
    for design_index, design in enumerate(designs or []):
        if design.get("written") and not design.get("needs_resync"):
            continue
        ids = design.get("sequence_ids", []) or []
        for segment_index, segment in enumerate(design.get("segments", []) or []):
            edges = [e for raw in segment.get("edge_sequence", []) or [] if (e := _base._canonical_edge(raw))]
            if not edges:
                continue
            nodes = _base._extract_route_graph_nodes(segment, work_crs, edge_crs, edge_crs)
            if len(nodes) != len(edges) + 1:
                continue
            for node_index, node in enumerate(nodes):
                item = None
                if node_index == 0 and segment_index < len(ids):
                    item = ids[segment_index]
                elif node_index == len(nodes) - 1 and segment_index + 1 < len(ids):
                    item = ids[segment_index + 1]
                users.setdefault(_node_key(node), []).append({
                    "design_index": design_index,
                    "segment_index": segment_index,
                    "node_index": node_index,
                    "node_count": len(nodes),
                    "special": _special(item),
                    "kind": _kind(item),
                })
    return users


def _plan(designs, dc_layer, edge_layer, spacing):
    edge_crs = edge_layer.crs()
    work_crs = _metric_crs(edge_layer, dc_layer)
    pending, routes = _collect_uses(designs, edge_crs, work_crs)
    ordered = sorted(
        routes,
        key=lambda d: (
            -float(routes[d].get("priority_score", 0.0)),
            -float(routes[d].get("longest_directional_run", 0.0)),
            -float(routes[d].get("route_length", 0.0)),
            int(d),
        ),
    )
    occupancy = _occupancy(dc_layer, designs, work_crs)
    spatial_index, geometries = _base._build_existing_index(occupancy, work_crs)
    reserved_cache, used_by_edge = {}, {}
    slots = {}
    route_uses = {d: [] for d in ordered}
    for use in pending:
        route_uses.setdefault(use.design_index, []).append(use)

    # An ordinary Pole can be landed by only one independent Link.  Special
    # engineering nodes are explicitly allowed to have multiple cable uses.
    owners = {}
    rank = {d: i for i, d in enumerate(ordered)}
    for key, occurrences in _node_users(designs, edge_crs, work_crs).items():
        ordinary = [x for x in occurrences if not x["special"]]
        if ordinary:
            owners[key] = min(ordinary, key=lambda x: rank.get(x["design_index"], 999999))["design_index"]

    def reserved(edge):
        if edge not in reserved_cache:
            a, b = _base._edge_points(edge)
            a, b = _tp(a, edge_crs, work_crs), _tp(b, edge_crs, work_crs)
            reserved_cache[edge] = _base._existing_slot_occupancy(
                QgsGeometry.fromPolylineXY([a, b]), spacing, spatial_index, geometries
            )
        return set(reserved_cache[edge])

    for design_index in ordered:
        previous = None
        previous_segment = None
        for use in sorted(route_uses.get(design_index, []), key=lambda x: (x.segment_index, x.edge_index)):
            if previous_segment is not None and use.segment_index != previous_segment:
                previous = None
            edge = use.edge_key
            used = used_by_edge.setdefault(edge, reserved(edge))
            a, b = _base._edge_points(edge)
            a, b = _tp(a, edge_crs, work_crs), _tp(b, edge_crs, work_crs)
            ka, kb = _node_key(a), _node_key(b)
            candidates = []
            if previous is None and 0 not in used and owners.get(ka, design_index) == design_index and owners.get(kb, design_index) == design_index:
                candidates.append(0)
            if previous is not None:
                previous = int(previous)
                candidates.append(previous)
                sign = 1 if previous > 0 else -1 if previous < 0 else (1 if use.side_hint >= 0 else -1)
                candidates.extend(sign * n for n in range(max(1, abs(previous) + 1), 101))
                candidates.extend(-sign * n for n in range(1, 101))
            else:
                sign = 1 if use.side_hint >= 0 else -1
                candidates.extend(sign * n for n in range(1, 101))
                candidates.extend(-sign * n for n in range(1, 101))
            chosen = next((int(c) for c in candidates if int(c) not in used), None)
            if chosen is None:
                chosen = (max([abs(int(x)) for x in used] + [0]) + 1) * (1 if use.side_hint >= 0 else -1)
            slots[(design_index, use.segment_index, use.edge_index)] = chosen
            use.slot = chosen
            used.add(chosen)
            previous = chosen
            previous_segment = use.segment_index

    _log(
        f"[route-plan] links={len(ordered)}; main=available-first; continuity=ON; "
        "group-outside=ON; pole-exclusivity=ON"
    )
    return edge_crs, work_crs, ordered, routes, slots


def _intersection(p1, p2, q1, q2):
    rx, ry = p2.x() - p1.x(), p2.y() - p1.y()
    sx, sy = q2.x() - q1.x(), q2.y() - q1.y()
    denominator = rx * sy - ry * sx
    scale = max(hypot(rx, ry) * hypot(sx, sy), 1.0)
    if abs(denominator) <= 1e-10 * scale:
        return None
    qpx, qpy = q1.x() - p1.x(), q1.y() - p1.y()
    t = (qpx * sy - qpy * sx) / denominator
    return QgsPointXY(p1.x() + t * rx, p1.y() + t * ry)


def _same_lane_join(node, prev_a, prev_b, next_a, next_b, slot, spacing):
    if slot == 0:
        return QgsPointXY(node)
    prev_t = _base._unit(prev_a, prev_b)
    next_t = _base._unit(next_a, next_b)
    distance = float(slot) * float(spacing)
    p1 = _base._offset_point(node, prev_t, distance)
    p2 = _base._offset_point(QgsPointXY(prev_a.x() + prev_t[0], prev_a.y() + prev_t[1]), prev_t, distance)
    q1 = _base._offset_point(node, next_t, distance)
    q2 = _base._offset_point(QgsPointXY(next_a.x() + next_t[0], next_a.y() + next_t[1]), next_t, distance)
    hit = _intersection(p1, p2, q1, q2)
    if hit is not None and hypot(hit.x() - node.x(), hit.y() - node.y()) <= max(3.0 * abs(distance), 5.0 * max(float(spacing), 0.01)):
        return hit
    return (
        _base._offset_point(node, prev_t, distance),
        _base._offset_point(node, next_t, distance),
    )


def _transition_angle(slot):
    magnitude = abs(int(slot))
    return 60.0 if magnitude <= 2 else 75.0 if magnitude <= 4 else 90.0


def _transition_run(distance, angle_deg):
    if distance <= 1e-12:
        return 0.0
    value = tan(radians(float(angle_deg)))
    return distance / value if abs(value) > 1e-12 else distance


def _transition_entry(a, b, slot, spacing):
    distance = abs(int(slot)) * float(spacing)
    if slot == 0 or distance <= 1e-12:
        return QgsPointXY(a)
    length = hypot(b.x() - a.x(), b.y() - a.y())
    if length <= 1e-12:
        return QgsPointXY(a)
    run = min(_transition_run(distance, _transition_angle(slot)), length * 0.45)
    center = _base._point_along(a, b, run / length)
    return _base._offset_point(center, _base._unit(a, b), int(slot) * float(spacing))


def _takeoff_entry(a, b, slot, spacing, control_distance):
    distance = int(slot) * float(spacing)
    if slot == 0 or abs(distance) <= 1e-12:
        return QgsPointXY(a)
    length = hypot(b.x() - a.x(), b.y() - a.y())
    if length <= 1e-12:
        return QgsPointXY(a)
    run = min(max(0.01, float(control_distance)), length * 0.45)
    center = _base._point_along(a, b, run / length)
    return _base._offset_point(center, _base._unit(a, b), distance)


def _geometry(segment, slots_by_edge, spacing, work_crs, edge_crs, control_distance):
    edges = [e for raw in segment.get("edge_sequence", []) or [] if (e := _base._canonical_edge(raw))]
    stored = segment.get("points", []) or []
    old = [_tp(QgsPointXY(float(p[0]), float(p[1])), edge_crs, work_crs) for p in stored if len(p) >= 2]
    if not edges or len(old) < 2:
        return old
    nodes = _base._extract_route_graph_nodes(segment, work_crs, edge_crs, edge_crs)
    if len(nodes) != len(edges) + 1:
        raise RuntimeError("Offset Core: edge_sequence 与 route nodes 数量不一致")
    slots = [int(slots_by_edge.get(i, 0)) for i in range(len(edges))]
    if not any(slots):
        return old
    result = []

    def add(point):
        point = QgsPointXY(point)
        if not result or hypot(result[-1].x() - point.x(), result[-1].y() - point.y()) > 1e-7:
            result.append(point)

    add(old[0])
    special_start = bool(segment.get("_odn_special_start"))
    for i, slot in enumerate(slots):
        a, b = nodes[i], nodes[i + 1]
        if i == 0:
            # NORMAL endpoint: direct to Pole.  Only an explicitly special
            # shared output receives the 0.30 m takeoff.
            if slot == 0 or not special_start:
                add(a)
            else:
                add(_takeoff_entry(a, b, slot, spacing, control_distance))
        if i > 0:
            previous = slots[i - 1]
            if previous == slot:
                if slot == 0:
                    add(a)
                else:
                    joined = _same_lane_join(a, nodes[i - 1], nodes[i], a, b, slot, spacing)
                    if isinstance(joined, tuple):
                        add(joined[0])
                        add(joined[1])
                    else:
                        add(joined)
            elif slot == 0:
                add(a)
            else:
                add(_transition_entry(a, b, slot, spacing))
        if i == len(slots) - 1:
            # NORMAL endpoint: direct arrival to destination Pole/node.  For a
            # special return output, the previous lane geometry is retained.
            add(b)
        elif slot == 0:
            add(b)
        else:
            add(_base._offset_point(b, _base._unit(a, b), slot * spacing))
    add(old[-1])
    return result


def _set_flags(designs):
    for design in designs or []:
        ids = design.get("sequence_ids", []) or []
        for i, segment in enumerate(design.get("segments", []) or []):
            segment["_odn_special_start"] = _special(ids[i]) if i < len(ids) else False
            segment["_odn_special_end"] = _special(ids[i + 1]) if i + 1 < len(ids) else False


def _validate(designs, edge_crs, work_crs):
    for design_index, design in enumerate(designs or []):
        for segment_index, segment in enumerate(design.get("segments", []) or []):
            edges = [e for raw in segment.get("edge_sequence", []) or [] if (e := _base._canonical_edge(raw))]
            if not edges:
                continue
            nodes = _base._extract_route_graph_nodes(segment, work_crs, edge_crs, edge_crs)
            if len(nodes) != len(edges) + 1:
                raise RuntimeError(f"Offset Core: Link {design_index} segment {segment_index} topology invalid")
            points = segment.get("points", []) or []
            if len(points) < 2:
                raise RuntimeError(f"Offset Core: Link {design_index} segment {segment_index} points 无效")
            work_points = [_tp(QgsPointXY(float(p[0]), float(p[1])), edge_crs, work_crs) for p in points]
            if len(work_points) > 1 and QgsGeometry.fromPolylineXY(work_points).length() > 200000:
                raise RuntimeError(f"Offset Core: Link {design_index} segment {segment_index} geometry abnormal")


def _feature_point(feature, layer, work_crs):
    try:
        return _tp(QgsPointXY(feature.geometry().centroid().asPoint()), layer.crs(), work_crs)
    except Exception:
        return None


def _crs(authid):
    if not authid:
        return None
    try:
        value = QgsCoordinateReferenceSystem(str(authid))
        return value if value.isValid() else None
    except Exception:
        return None


def _fat_refs(designs):
    refs, duplicates = {}, set()
    for design_index, design in enumerate(designs or []):
        for pos, item in enumerate(design.get("sequence_ids", []) or []):
            if len(item) < 2 or _kind(item) != "FAT":
                continue
            try:
                fid = int(item[1])
            except Exception:
                continue
            if fid in refs:
                duplicates.add(fid)
            else:
                refs[fid] = {"design_index": design_index, "sequence_pos": pos}
    return refs, duplicates


def _nearest_on_route(design, anchor, edge_crs, work_crs):
    best = None
    for segment_index, segment in enumerate(design.get("segments", []) or []):
        raw = segment.get("points", []) or []
        if len(raw) < 2:
            continue
        points = [_tp(QgsPointXY(float(p[0]), float(p[1])), edge_crs, work_crs) for p in raw if len(p) >= 2]
        if len(points) < 2:
            continue
        geom = QgsGeometry.fromPolylineXY(points)
        nearest = geom.nearestPoint(QgsGeometry.fromPointXY(anchor))
        if nearest.isEmpty():
            continue
        point = QgsPointXY(nearest.asPoint())
        distance = hypot(point.x() - anchor.x(), point.y() - anchor.y())
        candidate = (distance, segment_index, point)
        if best is None or candidate < best:
            best = candidate
    return best


def _fat_target(ref, design, fat_feature, fat_layer, edge_crs, work_crs):
    segments = design.get("segments", []) or []
    pos = int(ref["sequence_pos"])
    incoming_index = pos - 1 if 0 <= pos - 1 < len(segments) else None
    outgoing_index = pos if 0 <= pos < len(segments) else None
    anchor = None
    if incoming_index is not None:
        nodes = _base._extract_route_graph_nodes(segments[incoming_index], work_crs, edge_crs, edge_crs)
        if nodes:
            anchor = QgsPointXY(nodes[-1])
    if anchor is None and outgoing_index is not None:
        nodes = _base._extract_route_graph_nodes(segments[outgoing_index], work_crs, edge_crs, edge_crs)
        if nodes:
            anchor = QgsPointXY(nodes[0])
    if anchor is None:
        anchor = _feature_point(fat_feature, fat_layer, work_crs)
    if anchor is None:
        return None, None, {"reason": "无法确定 FAT 锚点"}
    nearest = _nearest_on_route(design, anchor, edge_crs, work_crs)
    if nearest is None:
        return None, anchor, {"reason": "无法从最终 Offset Cable 几何确定 FAT 落点"}
    return nearest[2], anchor, {"segment_index": nearest[1], "distance": nearest[0]}


def _prepare_fat_moves(designs, fat_layer, edge_layer, work_crs, fat_max_distance_m):
    if fat_layer is None:
        return {}, {"total": 0, "skipped": 0, "corner": 0, "straight": 0}
    edge_crs = edge_layer.crs()
    features = {int(f.id()): f for f in fat_layer.getFeatures()}
    refs, duplicates = _fat_refs(designs)
    if duplicates:
        _log(f"[fat-landing] duplicate_fat_refs={sorted(duplicates)[:20]}", Qgis.Warning)
    moves = {}
    stats = {"total": len(refs), "skipped": 0, "corner": 0, "straight": 0}
    for fid, ref in refs.items():
        feature = features.get(fid)
        if feature is None:
            stats["skipped"] += 1
            _log(f"[fat-landing-skip] fid={fid}; reason=feature-not-found", Qgis.Warning)
            continue
        current = _feature_point(feature, fat_layer, work_crs)
        target, anchor, info = _fat_target(ref, designs[ref["design_index"]], feature, fat_layer, edge_crs, work_crs)
        if target is None or anchor is None:
            stats["skipped"] += 1
            _log(f"[fat-landing-skip] fid={fid}; reason={info.get('reason', 'unknown')}", Qgis.Warning)
            continue
        anchor_distance = hypot(current.x() - anchor.x(), current.y() - anchor.y()) if current else 0.0
        if anchor_distance > max(0.01, float(fat_max_distance_m)):
            stats["skipped"] += 1
            _log(f"[fat-landing-skip] fid={fid}; distance_to_pole={anchor_distance:.3f}m; limit={float(fat_max_distance_m):.3f}m", Qgis.Warning)
            continue
        target_edge = QgsPointXY(_tp(target, work_crs, edge_crs))
        target_layer = QgsPointXY(_tp(target_edge, edge_crs, fat_layer.crs()))
        move_distance = hypot(current.x() - target.x(), current.y() - target.y()) if current else 0.0
        moves[fid] = {
            "design_index": ref["design_index"], "sequence_pos": ref["sequence_pos"],
            "target_edge": target_edge, "target_layer": target_layer,
            "target_work": target, "anchor": anchor, "move_distance": move_distance,
            "mode": "final_route_geometry", "turn_angle": 0.0, "offset": None,
        }
        stats["straight"] += 1
        _log(f"[fat-landing] fid={fid}; design={ref['design_index']}; seq_pos={ref['sequence_pos']}; mode=final_route_geometry; move={move_distance:.3f}m")
    return moves, stats


def _replace_fat_endpoints(designs, moves, edge_crs):
    touched = set()
    for move in moves.values():
        di, pos = move["design_index"], move["sequence_pos"]
        if not (0 <= di < len(designs)):
            continue
        design = designs[di]
        source_crs = _crs(design.get("source_crs")) or edge_crs
        target = _tp(move["target_edge"], edge_crs, source_crs)
        segments = design.get("segments", []) or []
        incoming = pos - 1 if 0 <= pos - 1 < len(segments) else None
        outgoing = pos if 0 <= pos < len(segments) else None
        if incoming is not None:
            points = list(segments[incoming].get("points", []) or [])
            if points:
                points[-1] = [float(target.x()), float(target.y())]
                segments[incoming]["points"] = points
                touched.add(di)
        if outgoing is not None:
            points = list(segments[outgoing].get("points", []) or [])
            if points:
                points[0] = [float(target.x()), float(target.y())]
                segments[outgoing]["points"] = points
                touched.add(di)
    for di in touched:
        design = designs[di]
        total = 0.0
        for segment in design.get("segments", []) or []:
            points = segment.get("points", []) or []
            if len(points) >= 2:
                length = 0.0
                for i in range(1, len(points)):
                    length += hypot(float(points[i][0]) - float(points[i - 1][0]), float(points[i][1]) - float(points[i - 1][1]))
                segment["distance"] = round(length, 3)
                total += length
        design["length"] = round(total, 3)
    return len(touched)


def _apply_fat_moves(fat_layer, moves):
    moved = 0
    for fid, move in moves.items():
        feature = fat_layer.getFeature(int(fid))
        if not feature or not feature.isValid():
            raise RuntimeError(f"FAT feature {fid} 不存在，无法更新 FAT 落点")
        target = move.get("target_layer")
        if target is None:
            raise RuntimeError(f"FAT feature {fid} 的目标坐标无效")
        geometry = QgsGeometry.fromPointXY(QgsPointXY(target))
        if feature.geometry().distance(geometry) > 1e-9:
            feature.setGeometry(geometry)
            if not fat_layer.updateFeature(feature):
                raise RuntimeError(f"无法写入 FAT feature {fid} 的新落点")
            moved += 1
    return moved


def commit_fat_landing_points(fat_layer, summary):
    moves = (summary or {}).get("fat_moves") or {}
    moved = _apply_fat_moves(fat_layer, moves)
    summary["fat_written"] = moved
    return moved


def apply_offset_layout(
    designs, distribution_layer, edge_layer, spacing=DEFAULT_SPACING_M,
    control_distance_m=DEFAULT_CONTROL_M, fat_layer=None,
    fat_max_distance_m=DEFAULT_FAT_MAX_DISTANCE_M,
):
    spacing = max(0.01, float(spacing))
    control_distance_m = max(0.01, float(control_distance_m))
    _set_flags(designs)
    edge_crs, work_crs, ordered, routes, slot_map = _plan(designs, distribution_layer, edge_layer, spacing)
    changed = set()
    extra_length = 0.0
    for design_index, design in enumerate(designs or []):
        if design.get("written") and not design.get("needs_resync"):
            continue
        new_segments = []
        total = 0.0
        difference = False
        for segment_index, segment in enumerate(design.get("segments", []) or []):
            edge_count = len(segment.get("edge_sequence", []) or [])
            by_edge = {i: slot_map.get((design_index, segment_index, i), 0) for i in range(edge_count)}
            old = [_tp(QgsPointXY(float(p[0]), float(p[1])), edge_crs, work_crs) for p in segment.get("points", []) if len(p) >= 2]
            new = _geometry(segment, by_edge, spacing, work_crs, edge_crs, control_distance_m)
            copy_segment = dict(segment)
            if len(new) >= 2:
                geometry = QgsGeometry.fromPolylineXY(new)
                old_geometry = QgsGeometry.fromPolylineXY(old) if len(old) >= 2 else QgsGeometry()
                if len(new) != len(old) or any(hypot(a.x() - b.x(), a.y() - b.y()) > 1e-7 for a, b in zip(new, old)):
                    difference = True
                new_length = geometry.length()
                old_length = old_geometry.length() if not old_geometry.isEmpty() else new_length
                extra_length += max(0.0, new_length - old_length)
                copy_segment["points"] = [[float(_tp(p, work_crs, edge_crs).x()), float(_tp(p, work_crs, edge_crs).y())] for p in new]
                copy_segment["distance"] = round(new_length, 3)
                copy_segment["layout_spacing"] = round(spacing, 3)
                new_segments.append(copy_segment)
                total += new_length
            else:
                new_segments.append(copy_segment)
                total += float(copy_segment.get("distance", 0.0) or 0.0)
        if difference:
            design["segments"] = new_segments
            design["length"] = round(total, 3)
            changed.add(design_index)
        design["source_crs"] = edge_crs.authid()
        design["layout"] = {
            "version": 21, "engine": "OffsetCore", "work_crs": work_crs.authid(),
            "spacing_m": round(spacing, 3), "fanout_control_distance_m": round(control_distance_m, 3),
            "main_lane": "complete_route_priority_available_0",
            "lane": "relative_continuity_group_outside",
            "pole": "one_independent_cable_landing",
            "corner": "continuous_offset",
            "takeoff": "special_endpoint_only",
            "fat": "owning_link_final_geometry",
        }

    # FAT landing is part of this engine, not a downstream legacy patch.
    fat_moves, fat_stats = _prepare_fat_moves(
        designs, fat_layer, edge_layer, work_crs, fat_max_distance_m
    ) if fat_layer is not None else ({}, {"total": 0, "skipped": 0, "corner": 0, "straight": 0})
    endpoint_updates = _replace_fat_endpoints(designs, fat_moves, edge_crs) if fat_moves else 0
    _validate(designs, edge_crs, work_crs)
    summary = {
        "changed_designs": len(changed), "changed_indices": sorted(changed),
        "spacing_m": spacing, "extra_length_m": round(extra_length, 3),
        "version": 21, "work_crs": work_crs.authid(), "lane_allocator": "OffsetCore",
        "priority": ordered, "slot_map": {str(k): int(v) for k, v in slot_map.items()},
        "corner_geometry": "continuous_offset", "fanout_control_distance_m": control_distance_m,
        "fat_moves": fat_moves, "fat_total": fat_stats["total"],
        "fat_skipped": fat_stats["skipped"], "fat_corner": fat_stats["corner"],
        "fat_straight": fat_stats["straight"], "fat_endpoint_updates": endpoint_updates,
        "fat_max_distance_m": float(fat_max_distance_m),
    }
    _log(
        f"[OffsetCore] links={len(ordered)}; changed={len(changed)}; spacing={spacing:.3f}m; "
        f"control={control_distance_m:.3f}m; ordinary_corner_control=NOT_USED; "
        f"fat={fat_stats['total']}; fat_skipped={fat_stats['skipped']}"
    )
    return summary
