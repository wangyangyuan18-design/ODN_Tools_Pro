# -*- coding: utf-8 -*-
"""Authoritative ODN cable offset engine.

Lane planning is kept separate from final cable geometry.
The geometry stage explicitly decides the corner behaviour before building
any final cable vertices.  Physical Pole nodes are topology references only;
ordinary cable corners are independent geometry points.
"""

from math import acos, degrees, hypot, tan, radians

from qgis.PyQt.QtCore import QSettings
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsGeometry,
    QgsMessageLog,
    QgsPointXY,
    QgsSpatialIndex,
    QgsUnitTypes,
    QgsVectorLayer,
    Qgis,
)

from . import cable_offset_layout as _base

LOG_TAG = "ODN_Tools_Pro / Cable Offset"


FAT_TRACE_DESIGN = "DAR463_H1A1"
FAT_TRACE_LINKS = {"L3", "L4"}
FAT_TRACE_NODE = "N0020"
FAT_TRACE_FAT = "FTTx DAR463_H1A1_CH3_ODP1"
FAT_TRACE_EPS_M = 0.05

# This update is intentionally limited to the pole-first geometry invariant.
# The repository's existing lane allocation / route priority / exclusivity
# rules remain authoritative everywhere else.
POLE_FIRST_EPS_M = 1e-7


def _fat_trace_point(point):
    try:
        return f"({float(point.x()):.6f},{float(point.y()):.6f})"
    except Exception:
        return str(point)


def _fat_trace_feature_name(feature):
    try:
        index = feature.fields().indexOf("Name")
        return str(feature[index] or "") if index >= 0 else ""
    except Exception:
        return ""


def _fat_trace_item_text(item):
    if isinstance(item, (list, tuple)):
        return " ".join(str(x) for x in item).upper()
    return str(item).upper()


def _fat_trace_seq_match(item):
    return FAT_TRACE_NODE in _fat_trace_item_text(item)


def _fat_trace_link_match(design):
    if not isinstance(design, dict):
        return False
    link = str(design.get("link", "")).upper().strip()
    text = " ".join(
        str(design.get(k, ""))
        for k in ("name", "fdt", "link", "_link_id")
    ).upper()
    return FAT_TRACE_DESIGN in text and link in FAT_TRACE_LINKS


def _fat_trace_focus(design, feature_name=""):
    if not _fat_trace_link_match(design):
        return False
    return any(_fat_trace_seq_match(x) for x in (design.get("sequence_ids", []) or [])) or FAT_TRACE_FAT.upper() in str(feature_name).upper()
DEFAULT_SPACING_M = 0.50
DEFAULT_CONTROL_M = 0.30
DEFAULT_FAT_MAX_DISTANCE_M = 3.0
SPACING_KEY = "ODNToolsPro/CableOffsetLayout/spacing_m"
CONTROL_DISTANCE_KEY = "ODNToolsPro/CableOffsetLayout/control_distance_m"

SHARED_NODE_TYPES = {"FDT", "BB", "CL", "CLOSURE", "SFCCL", "SFCCLOSURE", "FATRETURN"}
ENDPOINT_TYPES = SHARED_NODE_TYPES | {"FAT"}

# Ordinary corner decisions.  These names intentionally mirror the engineering
# terminology used by the offset rules/documentation.
CORNER_EARLY_TURN = "EARLY_TURN"          # D
CORNER_CROSS_MAIN_TURN = "CROSS_MAIN_TURN"  # E
CORNER_MAIN_REACH_TURN = "MAIN_REACH_TURN"  # F
CORNER_SAME_LANE_TURN = "SAME_LANE_TURN"
CORNER_RETURN_TURN = "RETURN_TURN"        # special return/takeoff context


def _log(message, level=Qgis.Info):
    QgsMessageLog.logMessage(str(message), LOG_TAG, level)


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
    return "".join(ch for ch in str(item[0]).strip().upper() if ch.isalnum()) if item else ""


def _shared_node(item):
    kind = _kind(item)
    return (
        kind.startswith("FDT")
        or kind.startswith("BB")
        or kind in {"CL", "CLOSURE"}
        or kind.startswith("SFCCL")
        or kind.startswith("SFCCLOSURE")
        or kind.startswith("FATRETURN")
    )


def _endpoint_special(item):
    kind = _kind(item)
    return _shared_node(item) or kind.startswith("FAT")


def _metric_crs(edge_layer, dc=None):
    for layer in (dc, edge_layer):
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


def _tp(point, source, target):
    return _base._transform_point(QgsPointXY(point), source, target)


def _node_key(point):
    return round(float(point.x()), 7), round(float(point.y()), 7)


def _turn_angle(a, b, c):
    v1 = _base._unit(a, b)
    v2 = _base._unit(b, c)
    dot = max(-1.0, min(1.0, v1[0] * v2[0] + v1[1] * v2[1]))
    return degrees(acos(dot))


def _route_metrics(design_index, design, edge_crs, work_crs):
    total = longest = current = turn_sum = 0.0
    turns = reversals = edges_count = 0
    seen = set()
    for segment in design.get("segments", []) or []:
        edges = [e for raw in segment.get("edge_sequence", []) or [] if (e := _base._canonical_edge(raw))]
        if not edges:
            continue
        nodes = _base._extract_route_graph_nodes(segment, work_crs, edge_crs, edge_crs)
        if len(nodes) != len(edges) + 1:
            continue
        for index, edge in enumerate(edges):
            a, b = _base._edge_points(edge)
            a = _tp(a, edge_crs, work_crs)
            b = _tp(b, edge_crs, work_crs)
            length = hypot(b.x() - a.x(), b.y() - a.y())
            edges_count += 1
            if edge not in seen:
                total += length
                seen.add(edge)
            current += length
            if index + 1 < len(edges):
                angle = _turn_angle(nodes[index], nodes[index + 1], nodes[index + 2])
                turns += 1
                turn_sum += angle
                reversals += int(angle >= 135.0)
                if angle >= 25.0:
                    longest = max(longest, current)
                    current = 0.0
    longest = max(longest, current)
    score = longest * 1000.0 + total * 10.0 - turns * 150.0 - reversals * 500.0
    return {
        "route_length": total,
        "longest_directional_run": longest,
        "turn_count": turns,
        "turn_sum": turn_sum,
        "reversal_count": reversals,
        "edge_count": edges_count,
        "priority_score": score,
    }


def _collect_uses(designs, edge_crs, work_crs):
    pending = []
    routes = {}
    for design_index, design in enumerate(designs or []):
        if design.get("written") and not design.get("needs_resync"):
            continue
        routes[design_index] = _route_metrics(design_index, design, edge_crs, work_crs)
        for segment_index, segment in enumerate(design.get("segments", []) or []):
            edges = [e for raw in segment.get("edge_sequence", []) or [] if (e := _base._canonical_edge(raw))]
            points = segment.get("points", []) or []
            if not edges or len(points) < 2:
                continue
            nodes = _base._extract_route_graph_nodes(segment, work_crs, edge_crs, edge_crs)
            if len(nodes) != len(edges) + 1:
                continue
            start = _tp(
                QgsPointXY(float(points[0][0]), float(points[0][1])), edge_crs, work_crs
            )
            end = _tp(
                QgsPointXY(float(points[-1][0]), float(points[-1][1])), edge_crs, work_crs
            )
            for edge_index, edge in enumerate(edges):
                a, b = _base._edge_points(edge)
                a = _tp(a, edge_crs, work_crs)
                b = _tp(b, edge_crs, work_crs)
                use = _base._LayoutUse(
                    design_index,
                    segment_index,
                    edge_index,
                    edge,
                    start,
                    end,
                    _base._route_side_hint(start, end, a, b),
                )
                use.prev_edge_key = edges[edge_index - 1] if edge_index else None
                pending.append(use)
    return pending, routes


def _occupancy(dc, designs, work):
    memory = QgsVectorLayer(
        f"LineString?crs={work.authid()}",
        "ODN Offset Occupancy",
        "memory",
    )
    index_field = dc.fields().indexOf("_ODN_LINK_ID")
    current_ids = {str(d.get("_link_id")) for d in designs or [] if d.get("_link_id")}
    for feature in dc.getFeatures():
        try:
            if index_field >= 0 and feature.attribute(index_field) and str(feature.attribute(index_field)) in current_ids:
                continue
            geometry = _base._transform_geometry(feature.geometry(), dc.crs(), work)
            if geometry.isEmpty():
                continue
            clone = QgsFeature()
            clone.setGeometry(geometry)
            memory.dataProvider().addFeature(clone)
        except Exception:
            pass
    memory.updateExtents()
    return memory


def _node_users(designs, edge_crs, work):
    users = {}
    for design_index, design in enumerate(designs or []):
        if design.get("written") and not design.get("needs_resync"):
            continue
        sequence_ids = design.get("sequence_ids", []) or []
        for segment_index, segment in enumerate(design.get("segments", []) or []):
            edges = [e for raw in segment.get("edge_sequence", []) or [] if (e := _base._canonical_edge(raw))]
            if not edges:
                continue
            nodes = _base._extract_route_graph_nodes(segment, work, edge_crs, edge_crs)
            if len(nodes) != len(edges) + 1:
                continue
            for node_index, point in enumerate(nodes):
                item = (
                    sequence_ids[segment_index]
                    if node_index == 0 and segment_index < len(sequence_ids)
                    else sequence_ids[segment_index + 1]
                    if node_index == len(nodes) - 1 and segment_index + 1 < len(sequence_ids)
                    else None
                )
                if item is None:
                    continue
                key = _node_key(point)
                users.setdefault(key, []).append(
                    {
                        "design_index": design_index,
                        "segment_index": segment_index,
                        "node_index": node_index,
                        "node_count": len(nodes),
                        "special": _shared_node(item),
                        "kind": _kind(item),
                    }
                )
    return users


def _validate_pole_exclusivity(designs, edge_crs, work):
    conflicts = []
    for key, items in _node_users(designs, edge_crs, work).items():
        ordinary = [item for item in items if not item["special"]]
        independent = {item["design_index"] for item in ordinary}
        if len(independent) > 1:
            conflicts.append((key, sorted(independent)))
    if conflicts:
        details = []
        for key, design_ids in conflicts[:20]:
            labels = [
                str(designs[index].get("_link_id", designs[index].get("name", index)))
                for index in design_ids
                if 0 <= index < len(designs)
            ]
            details.append(f"node={key}; links={labels}")
        raise RuntimeError(
            "Offset Core: 普通 Pole 只允许 1 条独立 Cable 连接；发现共点冲突："
            + " | ".join(details)
        )


def _plan(designs, dc, edge_layer, spacing):
    """Global lane allocation with compact relative lanes and stable physical sides.

    Lane index is a relative position, not a permanent absolute identifier.
    Existing members compress toward the Main Lane when membership changes,
    while preserving each cable's physical left/right side whenever possible.
    """
    edge_crs = edge_layer.crs()
    work = _metric_crs(edge_layer, dc)
    pending, routes = _collect_uses(designs, edge_crs, work)
    _validate_pole_exclusivity(designs, edge_crs, work)

    ordered = sorted(
        routes,
        key=lambda index: (
            -float(routes[index].get("priority_score", 0)),
            -float(routes[index].get("longest_directional_run", 0)),
            -float(routes[index].get("route_length", 0)),
            int(index),
        ),
    )
    rank = {index: order for order, index in enumerate(ordered)}
    occupancy = _occupancy(dc, designs, work)
    spatial_index, geometries = _base._build_existing_index(occupancy, work)

    reserved_cache = {}
    slots = {}
    uses_by_edge = {}
    for use in pending:
        uses_by_edge.setdefault(use.edge_key, []).append(use)
    for uses in uses_by_edge.values():
        uses.sort(key=lambda use: (rank.get(use.design_index, 999999), use.segment_index, use.edge_index))

    def reserved(edge):
        if edge not in reserved_cache:
            a, b = _base._edge_points(edge)
            a = _tp(a, edge_crs, work)
            b = _tp(b, edge_crs, work)
            reserved_cache[edge] = _base._existing_slot_occupancy(
                QgsGeometry.fromPolylineXY([a, b]), spacing, spatial_index, geometries
            )
        return set(reserved_cache[edge])

    def edge_order_key(edge):
        return min(
            (rank.get(use.design_index, 999999), use.segment_index, use.edge_index)
            for use in uses_by_edge[edge]
        )

    def previous_slot(use):
        if use.edge_index <= 0:
            return None
        return slots.get((use.design_index, use.segment_index, use.edge_index - 1))

    slot_changes = 0
    continuity_preserved = 0
    side_stable = 0
    compressed_edges = 0

    for edge in sorted(uses_by_edge, key=edge_order_key):
        uses = uses_by_edge[edge]
        reserved_slots = reserved(edge)
        used = set(reserved_slots)
        assigned = {}

        # Main Lane: keep exactly one primary cable on slot 0 whenever
        # the physical Main Lane is not already occupied by an external DC.
        primary = None
        if 0 not in reserved_slots:
            previous_zero = [use for use in uses if previous_slot(use) == 0]
            if previous_zero:
                primary = min(
                    previous_zero,
                    key=lambda use: (rank.get(use.design_index, 999999), use.segment_index, use.edge_index),
                )
            elif uses:
                primary = uses[0]
        if primary is not None:
            key = (primary.design_index, primary.segment_index, primary.edge_index)
            assigned[key] = 0
            used.add(0)

        non_main = [
            use for use in uses
            if (use.design_index, use.segment_index, use.edge_index) not in assigned
        ]

        side_groups = {1: [], -1: []}
        for use in non_main:
            previous = previous_slot(use)
            if previous is not None and previous != 0:
                side = 1 if previous > 0 else -1
            else:
                side = 1 if use.side_hint > 0 else -1
            side_groups[side].append((use, previous))

        for side in (1, -1):
            members = side_groups[side]
            members.sort(
                key=lambda item: (
                    0 if item[1] not in (None, 0) else 1,
                    abs(int(item[1])) if item[1] not in (None, 0) else 10**9,
                    rank.get(item[0].design_index, 999999),
                    item[0].segment_index,
                    item[0].edge_index,
                )
            )

            magnitude = 1
            for use, previous in members:
                while side * magnitude in used:
                    magnitude += 1
                chosen = side * magnitude
                key = (use.design_index, use.segment_index, use.edge_index)
                assigned[key] = chosen
                used.add(chosen)
                if previous is not None and previous != 0:
                    if (1 if previous > 0 else -1) == side:
                        side_stable += 1
                    if previous == chosen:
                        continuity_preserved += 1
                magnitude += 1

        for use in uses:
            key = (use.design_index, use.segment_index, use.edge_index)
            if key in assigned:
                continue
            side = 1 if use.side_hint > 0 else -1
            magnitude = 1
            while side * magnitude in used:
                magnitude += 1
            assigned[key] = side * magnitude
            used.add(side * magnitude)

        non_zero = sorted(abs(int(value)) for value in assigned.values() if int(value) != 0)
        if non_zero and non_zero != list(range(1, len(non_zero) + 1)):
            compressed_edges += 1

        for use in uses:
            key = (use.design_index, use.segment_index, use.edge_index)
            chosen = int(assigned[key])
            prior = previous_slot(use)
            slots[key] = chosen
            use.slot = chosen
            if prior is not None and prior != chosen:
                slot_changes += 1

        planned_zero = sum(
            1
            for use in uses
            if slots.get((use.design_index, use.segment_index, use.edge_index)) == 0
        )
        if 0 not in reserved_slots and uses and planned_zero != 1:
            raise RuntimeError(
                f"Offset Core: Pole Edge main-lane invariant violated for edge {edge}: planned_zero={planned_zero}"
            )
        if 0 in reserved_slots and planned_zero:
            raise RuntimeError(
                f"Offset Core: Pole Edge has duplicate main-lane ownership for edge {edge}"
            )

    _log(
        f"[route-plan] links={len(ordered)}; main=EXACTLY_ONE_SLOT0_PER_EDGE; "
        f"relative=COMPACT_RELATIVE_POSITION; side-stability=ON; "
        f"spacing={float(spacing):.3f}m; pole-exclusivity=ON; "
        f"slot-changes={slot_changes}; preserved={continuity_preserved}; "
        f"side-stable={side_stable}; compressed={compressed_edges}"
    )
    return edge_crs, work, ordered, routes, slots


def _corner_debug_point(point):
    try:
        return f"({float(point.x()):.6f},{float(point.y()):.6f})"
    except Exception:
        return str(point)


def _corner_debug_points(points):
    return "[" + ", ".join(_corner_debug_point(point) for point in (points or [])) + "]"


def _safe_unit(a, b):
    vector = _base._unit(a, b)
    if abs(vector[0]) <= 1e-12 and abs(vector[1]) <= 1e-12:
        return 1.0, 0.0
    return vector


def _line_intersection(p1, p2, q1, q2):
    rx = p2.x() - p1.x()
    ry = p2.y() - p1.y()
    sx = q2.x() - q1.x()
    sy = q2.y() - q1.y()
    denominator = rx * sy - ry * sx
    scale = max(hypot(rx, ry) * hypot(sx, sy), 1.0)
    if abs(denominator) <= 1e-10 * scale:
        return None
    qpx = q1.x() - p1.x()
    qpy = q1.y() - p1.y()
    parameter = (qpx * sy - qpy * sx) / denominator
    return QgsPointXY(p1.x() + parameter * rx, p1.y() + parameter * ry)


def _offset_line_through_node(node, direction, slot, spacing):
    anchor = _base._offset_point(node, direction, float(slot) * float(spacing))
    return anchor, QgsPointXY(anchor.x() + direction[0], anchor.y() + direction[1])


def _offset_lane_point(node, direction, slot, spacing):
    if int(slot) == 0:
        return QgsPointXY(node)
    return _base._offset_point(node, direction, float(slot) * float(spacing))


def _point_back(node, direction, run):
    return QgsPointXY(node.x() - direction[0] * run, node.y() - direction[1] * run)


def _point_forward(node, direction, run):
    return QgsPointXY(node.x() + direction[0] * run, node.y() + direction[1] * run)


def _clamped_run(edge_length, requested):
    if edge_length <= 1e-9:
        return 0.0
    return min(max(float(requested), 0.05), max(0.05, edge_length * 0.35))


def _corner_decision(prev_slot, next_slot, return_context=False):
    """Return the explicit D/E/F/SAME decision before geometry is generated."""
    prev_slot = int(prev_slot)
    next_slot = int(next_slot)

    if return_context:
        return CORNER_RETURN_TURN

    if prev_slot == next_slot:
        return CORNER_SAME_LANE_TURN
    if prev_slot == 0 and next_slot != 0:
        return CORNER_MAIN_REACH_TURN
    if prev_slot != 0 and next_slot == 0:
        return CORNER_EARLY_TURN
    if prev_slot * next_slot < 0:
        return CORNER_CROSS_MAIN_TURN
    if abs(next_slot) < abs(prev_slot):
        return CORNER_EARLY_TURN
    return CORNER_MAIN_REACH_TURN


def _lane_offset_line(node, direction, slot, spacing):
    """Return the true lateral Lane line through the route vertex."""
    anchor = _offset_lane_point(node, direction, slot, spacing)
    return anchor, QgsPointXY(anchor.x() + direction[0], anchor.y() + direction[1])


def _unified_lane_corner(node, in_a, in_b, out_a, out_b, prev_slot, next_slot, spacing):
    in_dir = _safe_unit(in_a, in_b)
    out_dir = _safe_unit(out_a, out_b)
    prev_slot = int(prev_slot)
    next_slot = int(next_slot)

    incoming_anchor, incoming_next = _lane_offset_line(node, in_dir, prev_slot, spacing)
    outgoing_anchor, outgoing_next = _lane_offset_line(node, out_dir, next_slot, spacing)

    if prev_slot == 0 and next_slot == 0:
        return [QgsPointXY(node)]

    # HARD INVARIANT: Main Lane slot 0 is the true physical Pole occupation
    # point.  Offset geometry is allowed only after that point is reached.
    if prev_slot == 0 and next_slot != 0:
        _log(
            f"[pole-first] MAIN_REACH: pole={_corner_debug_point(node)}; "
            f"next_slot={next_slot}; transition=AFTER_PHYSICAL_POLE"
        )
        return [QgsPointXY(node), QgsPointXY(outgoing_anchor)]

    if prev_slot != 0 and next_slot == 0:
        _log(
            f"[pole-first] REJOIN_MAIN: pole={_corner_debug_point(node)}; "
            f"prev_slot={prev_slot}; transition=INTO_PHYSICAL_POLE"
        )
        return [QgsPointXY(incoming_anchor), QgsPointXY(node)]

    hit = _line_intersection(
        incoming_anchor, incoming_next,
        outgoing_anchor, outgoing_next,
    )

    if hit is not None:
        distance = hypot(hit.x() - node.x(), hit.y() - node.y())
        max_reasonable = max(4.0 * float(spacing) * max(1, abs(prev_slot), abs(next_slot)), 2.0)
        if distance <= max_reasonable:
            return [hit]

    if hypot(incoming_anchor.x() - outgoing_anchor.x(), incoming_anchor.y() - outgoing_anchor.y()) > POLE_FIRST_EPS_M:
        return [incoming_anchor, outgoing_anchor]
    return [incoming_anchor]


def _same_lane_corner(node, in_a, in_b, out_a, out_b, slot, spacing):
    return _unified_lane_corner(node, in_a, in_b, out_a, out_b, slot, slot, spacing)


def _early_turn_corner(node, in_a, in_b, out_a, out_b, prev_slot, next_slot, spacing):
    return _unified_lane_corner(node, in_a, in_b, out_a, out_b, prev_slot, next_slot, spacing)


def _main_reach_corner(node, in_a, in_b, out_a, out_b, prev_slot, next_slot, spacing):
    return _unified_lane_corner(node, in_a, in_b, out_a, out_b, prev_slot, next_slot, spacing)


def _cross_main_corner(node, in_a, in_b, out_a, out_b, prev_slot, next_slot, spacing):
    return _unified_lane_corner(node, in_a, in_b, out_a, out_b, prev_slot, next_slot, spacing)


def _build_corner_geometry(node, in_edge, out_edge, prev_slot, next_slot, spacing, return_context=False):
    in_a, in_b = _edge_points_work(in_edge)
    out_a, out_b = _edge_points_work(out_edge)
    decision = _corner_decision(prev_slot, next_slot, return_context=return_context)

    if decision == CORNER_SAME_LANE_TURN:
        points = _same_lane_corner(node, in_a, in_b, out_a, out_b, prev_slot, spacing)
    elif decision == CORNER_EARLY_TURN:
        points = _early_turn_corner(node, in_a, in_b, out_a, out_b, prev_slot, next_slot, spacing)
    elif decision == CORNER_CROSS_MAIN_TURN:
        points = _cross_main_corner(node, in_a, in_b, out_a, out_b, prev_slot, next_slot, spacing)
    elif decision == CORNER_MAIN_REACH_TURN:
        points = _main_reach_corner(node, in_a, in_b, out_a, out_b, prev_slot, next_slot, spacing)
    else:
        points = _same_lane_corner(node, in_a, in_b, out_a, out_b, prev_slot, spacing)

    # Preserve the physical Pole whenever the cable transitions to/from Main
    # Lane.  Only corners entirely on non-zero Lanes use the old anti-collapse
    # filtering.  Main-Lane ownership is a stronger invariant than offset join.
    if int(prev_slot) != 0 and int(next_slot) != 0:
        filtered = [
            p for p in points
            if hypot(p.x() - node.x(), p.y() - node.y()) > POLE_FIRST_EPS_M
        ]
        if filtered:
            points = filtered

    _log(
        f"[corner-debug] BUILT; decision={decision}; "
        f"prev_slot={int(prev_slot)}; next_slot={int(next_slot)}; "
        f"node={_corner_debug_point(node)}; points={_corner_debug_points(points)}"
    )
    return decision, points


def _edge_points_work(edge):
    return _WORK_EDGE_POINTS.get(edge, _base._edge_points(edge))


_WORK_EDGE_POINTS = {}


def _takeoff_entry(a, b, slot, spacing, control):
    distance = int(slot) * float(spacing)
    if int(slot) == 0 or abs(distance) <= 1e-12:
        return QgsPointXY(a)
    length = hypot(b.x() - a.x(), b.y() - a.y())
    if length <= 1e-12:
        return QgsPointXY(a)
    run = min(max(0.01, float(control)), length * 0.45)
    center = _base._point_along(a, b, run / length)
    return _base._offset_point(center, _safe_unit(a, b), distance)


def _set_flags(designs):
    for design in designs or []:
        ids = design.get("sequence_ids", []) or []
        for index, segment in enumerate(design.get("segments", []) or []):
            segment["_odn_special_start"] = _endpoint_special(ids[index]) if index < len(ids) else False
            segment["_odn_special_end"] = _endpoint_special(ids[index + 1]) if index + 1 < len(ids) else False
            segment["_odn_return_start"] = _kind(ids[index]).startswith("FATRETURN") if index < len(ids) else False
            segment["_odn_return_end"] = _kind(ids[index + 1]).startswith("FATRETURN") if index + 1 < len(ids) else False


def _geometry(segment, slot_by_edge, spacing, work, edge_crs, control):
    edges = [e for raw in segment.get("edge_sequence", []) or [] if (e := _base._canonical_edge(raw))]
    stored = segment.get("points", []) or []
    old = [
        _tp(QgsPointXY(float(point[0]), float(point[1])), edge_crs, work)
        for point in stored
        if len(point) >= 2
    ]
    if not edges or len(old) < 2:
        return old

    nodes = _base._extract_route_graph_nodes(segment, work, edge_crs, edge_crs)
    if segment.get("_fat_trace_design"):
        seq = segment.get("_fat_trace_sequence_ids", []) or []
        seg_index = int(segment.get("_fat_trace_segment_index", -1))
        for seq_pos, item in enumerate(seq):
            if _fat_trace_seq_match(item):
                local_index = seq_pos - seg_index
                if 0 <= local_index < len(nodes):
                    _log(
                        f"[FAT-TRACE][04 ORIGINAL-NODE] link={segment.get('_fat_trace_link','')}; "
                        f"segment={seg_index}; node={FAT_TRACE_NODE}; "
                        f"node_index={local_index}; original={_fat_trace_point(nodes[local_index])}"
                    )
    if len(nodes) != len(edges) + 1:
        raise RuntimeError("Offset Core: edge_sequence 与 route nodes 数量不一致")

    slots = [int(slot_by_edge.get(index, 0)) for index in range(len(edges))]
    if not any(slots):
        return old

    _WORK_EDGE_POINTS.clear()
    for edge in edges:
        a, b = _base._edge_points(edge)
        _WORK_EDGE_POINTS[edge] = (_tp(a, edge_crs, work), _tp(b, edge_crs, work))

    result = []

    def add(point):
        point = QgsPointXY(point)
        if not result or hypot(result[-1].x() - point.x(), result[-1].y() - point.y()) > POLE_FIRST_EPS_M:
            result.append(point)

    add(old[0])
    special_start = bool(segment.get("_odn_special_start"))
    special_end = bool(segment.get("_odn_special_end"))
    return_start = bool(segment.get("_odn_return_start"))

    first_a = nodes[0]
    first_b = nodes[1]
    if slots[0] == 0 or not special_start:
        add(_offset_lane_point(first_a, _safe_unit(first_a, first_b), slots[0], spacing))
    else:
        add(_takeoff_entry(first_a, first_b, slots[0], spacing, control))
        if return_start:
            _log("[corner] RETURN_TURN at special return endpoint", Qgis.Info)

    corner_decisions = []

    for edge_index, slot in enumerate(slots):
        a = nodes[edge_index]
        b = nodes[edge_index + 1]
        direction = _safe_unit(a, b)

        if edge_index + 1 < len(slots):
            next_slot = slots[edge_index + 1]
            decision, corner_points = _build_corner_geometry(
                b,
                edges[edge_index],
                edges[edge_index + 1],
                slot,
                next_slot,
                spacing,
                return_context=bool(segment.get("_odn_return_end")) and edge_index + 1 == len(slots) - 1,
            )
            if segment.get("_fat_trace_design"):
                seq = segment.get("_fat_trace_sequence_ids", []) or []
                seg_index = int(segment.get("_fat_trace_segment_index", -1))
                node_positions = {pos - seg_index for pos, item in enumerate(seq) if _fat_trace_seq_match(item)}
                if (edge_index + 1) in node_positions:
                    _log(
                        f"[FAT-TRACE][05 FINAL-CORNER] link={segment.get('_fat_trace_link','')}; "
                        f"segment={seg_index}; node={FAT_TRACE_NODE}; edge={edge_index}; "
                        f"decision={decision}; physical_node={_fat_trace_point(b)}; "
                        f"corner={_corner_debug_points(corner_points)}"
                    )
                    segment["_fat_trace_final_corner"] = [
                        [float(p.x()), float(p.y())] for p in corner_points
                    ]
            corner_decisions.append(decision)
            _log(
                f"[corner] segment={segment.get('name', '')}; edge={edge_index}; "
                f"prev_slot={slot}; next_slot={next_slot}; decision={decision}"
            )
            _log(
                f"[corner-debug] FINAL-CORNER; link={segment.get('name', '')}; "
                f"edge={edge_index}; decision={decision}; "
                f"points={_corner_debug_points(corner_points)}"
            )
            for point in corner_points:
                add(point)
        else:
            if special_end:
                add(b)
                if segment.get("_fat_trace_design"):
                    _log(f"[FAT-TRACE][GEOMETRY-END] segment={segment.get('_fat_trace_segment_index')}; slot={slot}; special_end=TRUE; endpoint=PHYSICAL_NODE; node={_fat_trace_point(b)}")
            else:
                endpoint = _offset_lane_point(b, direction, slot, spacing)
                add(endpoint)
                if segment.get("_fat_trace_design"):
                    _log(f"[FAT-TRACE][GEOMETRY-END] segment={segment.get('_fat_trace_segment_index')}; slot={slot}; special_end=FALSE; endpoint=OFFSET_LANE; node={_fat_trace_point(b)}; endpoint={_fat_trace_point(endpoint)}")

    add(old[-1])
    segment["_corner_decisions"] = list(corner_decisions)
    return result


def _validate(designs, edge_crs, work):
    for design_index, design in enumerate(designs or []):
        for segment_index, segment in enumerate(design.get("segments", []) or []):
            edges = [e for raw in segment.get("edge_sequence", []) or [] if (e := _base._canonical_edge(raw))]
            if not edges:
                continue
            nodes = _base._extract_route_graph_nodes(segment, work, edge_crs, edge_crs)
            if len(nodes) != len(edges) + 1:
                raise RuntimeError(
                    f"Offset Core: Link {design_index} segment {segment_index} topology invalid"
                )
            if len(segment.get("points", []) or []) < 2:
                raise RuntimeError(
                    f"Offset Core: Link {design_index} segment {segment_index} points 无效"
                )


def _feature_point(feature, layer, work):
    try:
        return _tp(QgsPointXY(feature.geometry().centroid().asPoint()), layer.crs(), work)
    except Exception:
        return None


def _crs(authid):
    try:
        crs = QgsCoordinateReferenceSystem(str(authid))
        return crs if crs.isValid() else None
    except Exception:
        return None


def _fat_refs(designs):
    refs = {}
    duplicates = set()
    for design_index, design in enumerate(designs or []):
        for sequence_pos, item in enumerate(design.get("sequence_ids", []) or []):
            if len(item) < 2 or _kind(item) != "FAT":
                continue
            try:
                feature_id = int(item[1])
            except Exception:
                continue
            if feature_id in refs:
                duplicates.add(feature_id)
            else:
                refs[feature_id] = {
                    "design_index": design_index,
                    "sequence_pos": sequence_pos,
                }
    return refs, duplicates


def _nearest_on_route(design, anchor, edge_crs, work):
    best = None
    for segment_index, segment in enumerate(design.get("segments", []) or []):
        raw = segment.get("points", []) or []
        points = [
            _tp(QgsPointXY(float(point[0]), float(point[1])), edge_crs, work)
            for point in raw
            if len(point) >= 2
        ]
        if len(points) < 2:
            continue
        geometry = QgsGeometry.fromPolylineXY(points)
        nearest = geometry.nearestPoint(QgsGeometry.fromPointXY(anchor))
        if nearest.isEmpty():
            continue
        point = QgsPointXY(nearest.asPoint())
        distance = hypot(point.x() - anchor.x(), point.y() - anchor.y())
        candidate = (distance, segment_index, point)
        if best is None or candidate < best:
            best = candidate
    return best


def _fat_target(ref, design, feature, fat_layer, edge_crs, work):
    segments = design.get("segments", []) or []
    position = int(ref["sequence_pos"])
    incoming = position - 1 if 0 <= position - 1 < len(segments) else None
    outgoing = position if 0 <= position < len(segments) else None
    anchor = None

    if incoming is not None:
        nodes = _base._extract_route_graph_nodes(segments[incoming], work, edge_crs, edge_crs)
        if nodes:
            anchor = QgsPointXY(nodes[-1])
    if anchor is None and outgoing is not None:
        nodes = _base._extract_route_graph_nodes(segments[outgoing], work, edge_crs, edge_crs)
        if nodes:
            anchor = QgsPointXY(nodes[0])
    if anchor is None:
        anchor = _feature_point(feature, fat_layer, work)
    if anchor is None:
        return None, None, {"reason": "无法确定 FAT 锚点"}

    nearest = _nearest_on_route(design, anchor, edge_crs, work)
    if nearest is None:
        return None, anchor, {"reason": "无法从最终 Offset Cable 几何确定 FAT 落点"}
    fat_name = _fat_trace_feature_name(feature)
    if _fat_trace_link_match(design) and FAT_TRACE_FAT.upper() in fat_name.upper():
        source = "FINAL_ROUTE_SEGMENT"
        target = nearest[2]
        for seg_index, seg in enumerate(design.get("segments", []) or []):
            corner = seg.get("_fat_trace_final_corner")
            if not corner:
                continue
            distances = [
                (hypot(float(x)-target.x(), float(y)-target.y()), i, x, y)
                for i, (x, y) in enumerate(corner)
            ]
            if distances:
                distance, i, x, y = min(distances)
                if distance <= FAT_TRACE_EPS_M:
                    source = f"FINAL_CORNER segment={seg_index}; corner_index={i}; corner=({float(x):.6f},{float(y):.6f}); distance={distance:.6f}m"
        _log(
            f"[FAT-TRACE][06 TARGET] link={design.get('link','')}; feature_id={feature.id()}; "
            f"fat_name={fat_name}; route_segment={nearest[1]}; anchor={_fat_trace_point(anchor)}; "
            f"target={_fat_trace_point(target)}; source={source}"
        )
    return nearest[2], anchor, {
        "segment_index": nearest[1],
        "distance": nearest[0],
    }


def _prepare_fat_moves(designs, fat_layer, edge_layer, work, fat_limit):
    if fat_layer is None:
        return {}, {"total": 0, "skipped": 0, "corner": 0, "straight": 0}

    edge_crs = edge_layer.crs()
    features = {int(feature.id()): feature for feature in fat_layer.getFeatures()}
    references, duplicates = _fat_refs(designs)
    if duplicates:
        _log(
            f"[fat-landing] duplicate_fat_refs={sorted(duplicates)[:20]}",
            Qgis.Warning,
        )

    moves = {}
    stats = {
        "total": len(references),
        "skipped": 0,
        "corner": 0,
        "straight": 0,
    }

    for feature_id, reference in references.items():
        feature = features.get(int(feature_id))
        if feature is None:
            continue
        fat_name = _fat_trace_feature_name(feature)
        if FAT_TRACE_FAT.upper() not in fat_name.upper():
            continue
        owner_index = int(reference.get("design_index", -1))
        owner = designs[owner_index] if 0 <= owner_index < len(designs) else {}
        _log(
            f"[FAT-TRACE][01 DISCOVERED] fat={fat_name}; feature_id={feature_id}; "
            f"expected_owner=L3; ref_design_index={owner_index}; "
            f"ref_seq_pos={reference.get('sequence_pos')}"
        )
        _log(
            f"[FAT-TRACE][02 OWNER] fat={fat_name}; owner={owner.get('link','')}; "
            f"expected=L3; design={owner.get('_link_id', owner.get('name', ''))}"
        )

    for feature_id, reference in references.items():
        feature = features.get(int(feature_id))
        if feature is None:
            stats["skipped"] += 1
            continue
        current = _feature_point(feature, fat_layer, work)
        target, anchor, info = _fat_target(
            reference,
            designs[reference["design_index"]],
            feature,
            fat_layer,
            edge_crs,
            work,
        )
        trace_design = designs[reference["design_index"]]
        if _fat_trace_focus(trace_design, str(feature["Name"]) if feature.fields().indexOf("Name") >= 0 else ""):
            _log(f"[FAT-TRACE][DECISION] feature_id={feature_id}; link={trace_design.get('link','')}; current={_fat_trace_point(current)}; anchor={_fat_trace_point(anchor)}; target={_fat_trace_point(target) if target else 'None'}; info={info}")
        if target is None or anchor is None:
            if FAT_TRACE_FAT.upper() in _fat_trace_feature_name(feature).upper():
                _log(f"[FAT-TRACE][07 MOVE] feature_id={feature_id}; link={trace_design.get('link','')}; accepted=False; reason=NO_TARGET_OR_ANCHOR; info={info}")
            stats["skipped"] += 1
            continue

        anchor_distance = (
            hypot(current.x() - anchor.x(), current.y() - anchor.y())
            if current
            else 0.0
        )
        if anchor_distance > max(0.01, float(fat_limit)):
            if FAT_TRACE_FAT.upper() in _fat_trace_feature_name(feature).upper():
                _log(f"[FAT-TRACE][07 MOVE] feature_id={feature_id}; link={trace_design.get('link','')}; accepted=False; reason=ANCHOR_DISTANCE; anchor_distance={anchor_distance:.6f}; limit={float(fat_limit):.6f}")
            stats["skipped"] += 1
            continue

        target_edge = _tp(target, work, edge_crs)
        target_layer = _tp(target_edge, edge_crs, fat_layer.crs())
        if _fat_trace_focus(trace_design, str(feature["Name"]) if feature.fields().indexOf("Name") >= 0 else ""):
            _log(f"[FAT-TRACE][MOVE-PREP] feature_id={feature_id}; link={trace_design.get('link','')}; fat_name={feature['Name'] if feature.fields().indexOf('Name') >= 0 else ''}; current={_fat_trace_point(current)}; anchor={_fat_trace_point(anchor)}; target_edge={_fat_trace_point(target_edge)}; target_layer={_fat_trace_point(target_layer)}; anchor_distance={anchor_distance:.6f}; move_distance={hypot(current.x()-target.x(), current.y()-target.y()):.6f}; accepted=YES")
        if FAT_TRACE_FAT.upper() in _fat_trace_feature_name(feature).upper():
            _log(f"[FAT-TRACE][07 MOVE] feature_id={feature_id}; link={trace_design.get('link','')}; accepted=True; move_distance={hypot(current.x()-target.x(), current.y()-target.y()):.6f}; target={_fat_trace_point(target)}")
        moves[feature_id] = {
            "design_index": reference["design_index"],
            "sequence_pos": reference["sequence_pos"],
            "target_edge": QgsPointXY(target_edge),
            "target_layer": QgsPointXY(target_layer),
            "target_work": target,
            "anchor": anchor,
            "move_distance": hypot(current.x() - target.x(), current.y() - target.y()) if current else 0.0,
            "mode": "final_route_geometry",
            "route_decisions": designs[reference["design_index"]].get("_corner_decisions", []),
        }

        segment_index = info.get("segment_index")
        if isinstance(segment_index, int):
            segment = (designs[reference["design_index"]].get("segments", []) or [])[segment_index]
            if segment.get("_corner_decisions"):
                stats["corner"] += 1
            else:
                stats["straight"] += 1
        else:
            stats["straight"] += 1

    return moves, stats


def _replace_fat_endpoints(designs, moves, edge_crs):
    touched = set()
    for move in moves.values():
        design_index = move["design_index"]
        sequence_pos = move["sequence_pos"]
        if not 0 <= design_index < len(designs):
            continue
        design = designs[design_index]
        source = _crs(design.get("source_crs")) or edge_crs
        target = _tp(move["target_edge"], edge_crs, source)
        segments = design.get("segments", []) or []
        incoming = sequence_pos - 1 if 0 <= sequence_pos - 1 < len(segments) else None
        outgoing = sequence_pos if 0 <= sequence_pos < len(segments) else None

        if incoming is not None:
            points = list(segments[incoming].get("points", []) or [])
            if points:
                points[-1] = [float(target.x()), float(target.y())]
                segments[incoming]["points"] = points
                touched.add(design_index)
        if outgoing is not None:
            points = list(segments[outgoing].get("points", []) or [])
            if points:
                points[0] = [float(target.x()), float(target.y())]
                segments[outgoing]["points"] = points
                touched.add(design_index)

    for design_index in touched:
        total = 0.0
        design = designs[design_index]
        for segment in design.get("segments", []) or []:
            points = segment.get("points", []) or []
            length = (
                sum(
                    hypot(
                        float(points[index][0]) - float(points[index - 1][0]),
                        float(points[index][1]) - float(points[index - 1][1]),
                    )
                    for index in range(1, len(points))
                )
                if len(points) >= 2
                else 0.0
            )
            segment["distance"] = round(length, 3)
            total += length
        design["length"] = round(total, 3)
    return len(touched)


def _apply_fat_moves(layer, moves):
    moved = 0
    for feature_id, move in moves.items():
        feature = layer.getFeature(int(feature_id))
        target = move.get("target_layer")
        if feature is None or target is None:
            continue
        geometry = QgsGeometry.fromPointXY(QgsPointXY(target))
        if layer.changeGeometry(int(feature_id), geometry):
            moved += 1
    return moved


def _prepare_trace_context(designs):
    for design in designs or []:
        active = _fat_trace_link_match(design)
        design["_fat_trace_design"] = active
        if not active:
            continue
        sequence_ids = design.get("sequence_ids", []) or []
        _log(
            f"[FAT-TRACE][03 LINK-SEQ] link={design.get('link','')}; "
            f"sequence={sequence_ids}"
        )
        for seg_index, segment in enumerate(design.get("segments", []) or []):
            segment["_fat_trace_link"] = str(design.get("link", ""))
            segment["_fat_trace_segment_index"] = int(seg_index)
            segment["_fat_trace_sequence_ids"] = list(sequence_ids)


def _run_design(designs, dc, fat_layer, edge_layer):
    spacing, control = get_settings()
    _set_flags(designs)
    _prepare_trace_context(designs)
    edge_crs, work, ordered, routes, slots = _plan(
        designs, dc, edge_layer, spacing
    )
    slot_map = {}
    for design_index, segment_index, edge_index in slots:
        slot_map[(design_index, segment_index)] = {
            edge_index: slots[(design_index, segment_index, edge_index)]
            for edge_index in range(10_000)
            if (design_index, segment_index, edge_index) in slots
        }
    for design_index, design in enumerate(designs or []):
        for segment_index, segment in enumerate(design.get("segments", []) or []):
            segment_slots = slot_map.get((design_index, segment_index), {})
            segment["points"] = [
                [float(point.x()), float(point.y())]
                for point in _geometry(
                    segment,
                    segment_slots,
                    spacing,
                    work,
                    edge_crs,
                    control,
                )
            ]
        design["route_metrics"] = routes.get(design_index, {})
        design["_lane_slots"] = {
            str(key[2]): value
            for key, value in slots.items()
            if key[0] == design_index
        }

    _validate(designs, edge_crs, work)
    moves, fat_stats = _prepare_fat_moves(
        designs,
        fat_layer,
        edge_layer,
        work,
        DEFAULT_FAT_MAX_DISTANCE_M,
    )
    touched = _replace_fat_endpoints(designs, moves, edge_crs)
    return edge_crs, work, ordered, routes, slots, moves, fat_stats, touched


def build_offset_designs(designs, dc, fat_layer=None, edge_layer=None):
    if edge_layer is None:
        raise RuntimeError("Offset Core: Pole Edge 图层不能为空")
    if dc is None:
        raise RuntimeError("Offset Core: Distribution Cable 图层不能为空")
    working = [design for design in designs or []]
    if not working:
        return {
            "designs": [],
            "ordered": [],
            "routes": {},
            "slots": {},
            "moves": {},
            "fat_stats": {"total": 0, "skipped": 0, "corner": 0, "straight": 0},
            "touched": 0,
        }
    _log(
        f"[offset-core] START; designs={len(working)}; "
        f"spacing={get_settings()[0]:.3f}m; rules=route-priority+main-lane+relative-continuity+group-outside+pole-exclusivity"
    )
    edge_crs, work, ordered, routes, slots, moves, fat_stats, touched = _run_design(
        working, dc, fat_layer, edge_layer
    )
    return {
        "designs": working,
        "ordered": ordered,
        "routes": routes,
        "slots": slots,
        "moves": moves,
        "fat_stats": fat_stats,
        "touched": touched,
        "work_crs": work,
        "edge_crs": edge_crs,
    }
