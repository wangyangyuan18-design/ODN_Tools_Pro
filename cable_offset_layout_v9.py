# -*- coding: utf-8 -*-
"""Global lane priority v9 and route-aware continuous corner geometry.

Engineering lane rules:
- 0.50 m is the physical separation between parallel cable lanes.
- Lane index is a geometric relative position, not a Link number.
- A cable keeps its relative lane position through consecutive Pole Edges and
  corners whenever possible. Lane changes are only made for a real conflict,
  a route split/merge, or a deliberate destination-side change.
- When several cables run together, a newly joining cable is placed at the
  outside of the existing group on the side where it joins; it is not inserted
  between established cables.
- Ordinary Pole nodes are exclusive: two independent cable routes may not both
  terminate/connect at the same Pole node. Explicit engineering nodes such as
  FDT, FAT return, BB and SFC/CL are exceptions.
- Ordinary corners use pure lane-offset geometry. The 0.30 m control distance
  is reserved for same-point fan-out/return cases, not ordinary corners.
"""

from math import acos, degrees, hypot

from qgis.core import QgsGeometry, QgsPointXY, QgsMessageLog, Qgis

from . import cable_offset_layout as _base
from . import cable_offset_layout_v8 as _v8

LOG_TAG = "ODN_Tools_Pro / Cable Offset"


def _log(message, level=Qgis.Info):
    try:
        QgsMessageLog.logMessage(str(message), LOG_TAG, level)
    except Exception:
        pass


def _unit(a, b):
    return _base._unit(a, b)


def _turn_angle(a, b, c):
    v1 = _unit(a, b)
    v2 = _unit(b, c)
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
        raw_edges = segment.get("edge_sequence", []) or []
        edges = [e for raw in raw_edges if (e := _base._canonical_edge(raw))]
        if not edges:
            continue
        nodes = _base._extract_route_graph_nodes(segment, work_crs, edge_crs, edge_crs)
        if len(nodes) != len(edges) + 1:
            continue
        for i, edge in enumerate(edges):
            a, b = _base._edge_points(edge)
            a = _base._transform_point(a, edge_crs, work_crs)
            b = _base._transform_point(b, edge_crs, work_crs)
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


def _reserved_for_edge(edge_key, edge_crs, work_crs, spacing, existing_index, existing_geometries, cache):
    if edge_key in cache:
        return set(cache[edge_key])
    a, b = _base._edge_points(edge_key)
    a = _base._transform_point(a, edge_crs, work_crs)
    b = _base._transform_point(b, edge_crs, work_crs)
    reserved = _base._existing_slot_occupancy(
        QgsGeometry.fromPolylineXY([a, b]), spacing, existing_index, existing_geometries
    )
    cache[edge_key] = set(reserved)
    return set(reserved)


def _collect_uses(designs, edge_crs, work_crs):
    pending = []
    routes = {}
    for design_index, design in enumerate(designs or []):
        if design.get("written") and not design.get("needs_resync"):
            continue
        metrics = _route_metrics(design_index, design, edge_crs, work_crs)
        for segment_index, segment in enumerate(design.get("segments", []) or []):
            raw_edges = segment.get("edge_sequence", []) or []
            edges = [e for raw in raw_edges if (e := _base._canonical_edge(raw))]
            points = segment.get("points", []) or []
            if not edges or len(points) < 2:
                continue
            nodes = _base._extract_route_graph_nodes(segment, work_crs, edge_crs, edge_crs)
            if len(nodes) != len(edges) + 1:
                continue
            start_point = _base._transform_point(QgsPointXY(float(points[0][0]), float(points[0][1])), edge_crs, work_crs)
            end_point = _base._transform_point(QgsPointXY(float(points[-1][0]), float(points[-1][1])), edge_crs, work_crs)
            for edge_index, edge in enumerate(edges):
                a, b = _base._edge_points(edge)
                a = _base._transform_point(a, edge_crs, work_crs)
                b = _base._transform_point(b, edge_crs, work_crs)
                use = _base._LayoutUse(
                    design_index, segment_index, edge_index, edge,
                    start_point, end_point,
                    _base._route_side_hint(start_point, end_point, a, b),
                )
                if edge_index > 0:
                    use.prev_edge_key = edges[edge_index - 1]
                pending.append(use)
        routes[design_index] = metrics
    return pending, routes


def _same_side_lane_candidates(side_hint, limit=50):
    """Enumerate lanes from the requested side outward before crossing sides."""
    sign = 1 if side_hint >= 0 else -1
    for magnitude in range(1, limit + 1):
        yield sign * magnitude
    other = -sign
    for magnitude in range(1, limit + 1):
        yield other * magnitude


def _packed_lane_candidates(side_hint, limit=20):
    """Backward-compatible candidate generator with same-side preference."""
    yield 0
    yield from _same_side_lane_candidates(side_hint, limit)


def _compact_node_type(item):
    if not item or len(item) < 1:
        return ""
    text = str(item[0]).strip().upper()
    return "".join(ch for ch in text if ch.isalnum())


def _shared_node_exception(item):
    kind = _compact_node_type(item)
    if kind in {"FDT", "FAT", "BB", "CL", "CLOSURE", "SFCCL", "SFCCLOSURE"}:
        return True
    if kind.startswith("SFC") and ("CL" in kind or "CLOSURE" in kind):
        return True
    return False


def _node_key(point):
    return (round(float(point.x()), 7), round(float(point.y()), 7))


def _collect_node_users(designs, edge_crs, work_crs):
    users = {}
    for design_index, design in enumerate(designs or []):
        if design.get("written") and not design.get("needs_resync"):
            continue
        sequence_ids = design.get("sequence_ids", []) or []
        for segment_index, segment in enumerate(design.get("segments", []) or []):
            raw_edges = segment.get("edge_sequence", []) or []
            edges = [e for raw in raw_edges if (e := _base._canonical_edge(raw))]
            if not edges:
                continue
            nodes = _base._extract_route_graph_nodes(segment, work_crs, edge_crs, edge_crs)
            if len(nodes) != len(edges) + 1:
                continue
            for node_index, node in enumerate(nodes):
                seq_item = None
                if node_index == 0 and segment_index < len(sequence_ids):
                    seq_item = sequence_ids[segment_index]
                elif node_index == len(nodes) - 1 and segment_index + 1 < len(sequence_ids):
                    seq_item = sequence_ids[segment_index + 1]
                users.setdefault(_node_key(node), []).append({
                    "design_index": design_index,
                    "segment_index": segment_index,
                    "node_index": node_index,
                    "node_count": len(nodes),
                    "special": _shared_node_exception(seq_item),
                })
    return users


def _group_node_users_by_design(raw_users):
    grouped = {}
    for key, occurrences in raw_users.items():
        by_design = {}
        for occurrence in occurrences:
            di = occurrence["design_index"]
            entry = by_design.setdefault(di, {"special": False, "occurrences": []})
            entry["special"] = bool(entry["special"] or occurrence["special"])
            entry["occurrences"].append(occurrence)
        grouped[key] = by_design
    return grouped


def _use_map(pending):
    return {(use.design_index, use.segment_index, use.edge_index): use for use in pending}


def _slot_free_for_edge(edge_key, candidate, slot_map, use_lookup, excluded_keys, reserved_by_edge):
    if candidate in set(reserved_by_edge.get(edge_key, set())):
        return False
    for use_key, use in use_lookup.items():
        if use_key in excluded_keys:
            continue
        if use.edge_key == edge_key and int(slot_map.get(use_key, 0)) == int(candidate):
            return False
    return True


def _enforce_ordinary_node_exclusivity(designs, pending, slot_map, reserved_by_edge, priority_by_design, edge_crs, work_crs, spacing):
    node_groups = _group_node_users_by_design(_collect_node_users(designs, edge_crs, work_crs))
    use_lookup = _use_map(pending)
    changed = 0
    ordered_nodes = sorted(node_groups.items(), key=lambda item: min(priority_by_design.get(di, (float("inf"), di))[0] for di in item[1]))
    for node_key, by_design in ordered_nodes:
        if len(by_design) <= 1:
            continue
        keepers = sorted(by_design, key=lambda di: priority_by_design.get(di, (float("inf"), di)))
        keeper = keepers[0]
        for design_index in keepers:
            info = by_design[design_index]
            if info.get("special") or design_index == keeper:
                continue
            internal_occurrences = [occ for occ in info.get("occurrences", []) if 0 < int(occ["node_index"]) < int(occ["node_count"]) - 1]
            for occ in internal_occurrences:
                si = int(occ["segment_index"])
                ni = int(occ["node_index"])
                prev_key = (design_index, si, ni - 1)
                next_key = (design_index, si, ni)
                prev_use = use_lookup.get(prev_key)
                next_use = use_lookup.get(next_key)
                if prev_use is None or next_use is None:
                    continue
                prev_slot = int(slot_map.get(prev_key, 0))
                next_slot = int(slot_map.get(next_key, 0))
                if prev_slot != 0 and next_slot != 0:
                    continue
                excluded = {prev_key, next_key}
                hint = _base._route_side_hint(prev_use.start_point, prev_use.end_point, *_base._edge_points(prev_use.edge_key))
                chosen = None
                candidates = []
                for candidate in (prev_slot if prev_slot != 0 else None, next_slot if next_slot != 0 else None):
                    if candidate is not None and candidate != 0 and candidate not in candidates:
                        candidates.append(candidate)
                candidates.extend(c for c in _same_side_lane_candidates(hint, 100) if c not in candidates)
                for candidate in candidates:
                    if _slot_free_for_edge(prev_use.edge_key, candidate, slot_map, use_lookup, excluded, reserved_by_edge) and _slot_free_for_edge(next_use.edge_key, candidate, slot_map, use_lookup, excluded, reserved_by_edge):
                        chosen = int(candidate)
                        break
                if chosen is None:
                    continue
                slot_map[prev_key] = chosen
                slot_map[next_key] = chosen
                prev_use.slot = chosen
                next_use.slot = chosen
                changed += 1
                _log(f"[node-exclusive] node={node_key}; design={design_index}; keeper={keeper}; slot={prev_slot}->{chosen}/{next_slot}->{chosen}; reason=ordinary-node-already-used")
    return changed


def _lane_candidates_for_use(use, previous_slot, used, reserved, limit=100):
    """Choose a stable relative lane before considering a side change."""
    if previous_slot is not None:
        preferred_sign = 1 if previous_slot > 0 else -1 if previous_slot < 0 else (1 if use.side_hint >= 0 else -1)
        # Keep exactly the previous lane first.
        yield int(previous_slot)
        # If blocked, move outward on the same side. This preserves the
        # established cable group instead of inserting into its middle.
        magnitude = max(1, abs(int(previous_slot)) + 1)
        for m in range(magnitude, limit + 1):
            yield preferred_sign * m
        # Only after exhausting the original side may the allocator cross it.
        other = -preferred_sign
        for m in range(1, limit + 1):
            yield other * m
    else:
        # New cable joins from its actual side: occupy the outside of the
        # existing group on that side, never an already occupied inner lane.
        sign = 1 if use.side_hint >= 0 else -1
        for m in range(1, limit + 1):
            yield sign * m
        other = -sign
        for m in range(1, limit + 1):
            yield other * m
        yield 0


def make_global_slot_assigner(designs, distribution_layer, edge_layer, spacing, counters):
    edge_crs = edge_layer.crs()
    work_crs = _base._choose_work_crs(edge_layer)
    pending, routes = _collect_uses(designs, edge_crs, work_crs)
    users_by_edge = {}
    for use in pending:
        users_by_edge.setdefault(use.edge_key, []).append(use)

    ordered_designs = sorted(
        routes,
        key=lambda di: (
            -float(routes[di].get("priority_score", 0.0)),
            -float(routes[di].get("longest_directional_run", 0.0)),
            -float(routes[di].get("route_length", 0.0)),
            int(di),
        ),
    )
    priority_by_design = {di: (rank, di) for rank, di in enumerate(ordered_designs)}
    existing_index, existing_geometries = _base._build_existing_index(distribution_layer, work_crs)
    reserved_cache = {}
    reserved_by_edge = {}
    slot_map = {}

    # Allocate edge-by-edge, but carry the previous lane of each route into the
    # next edge. This is the key difference from the old independent-slot model.
    for edge_key, users in users_by_edge.items():
        reserved = _reserved_for_edge(edge_key, edge_crs, work_crs, spacing, existing_index, existing_geometries, reserved_cache)
        reserved_by_edge[edge_key] = set(reserved)
        used = set(reserved)
        for use in sorted(users, key=lambda item: priority_by_design.get(item.design_index, (float("inf"), item.design_index))):
            key = (use.design_index, use.segment_index, use.edge_index)
            previous_slot = None
            if use.edge_index > 0:
                previous_key = (use.design_index, use.segment_index, use.edge_index - 1)
                if previous_key in slot_map:
                    previous_slot = int(slot_map[previous_key])
            chosen = None
            for candidate in _lane_candidates_for_use(use, previous_slot, used, reserved):
                if candidate not in used:
                    chosen = int(candidate)
                    break
            if chosen is None:
                magnitude = max([abs(v) for v in used] + [0]) + 1
                sign = 1 if (previous_slot is None and use.side_hint >= 0) or (previous_slot is not None and previous_slot >= 0) else -1
                chosen = sign * magnitude
                while chosen in used:
                    magnitude += 1
                    chosen = sign * magnitude
            slot_map[key] = chosen
            use.slot = chosen
            used.add(chosen)

    node_changes = _enforce_ordinary_node_exclusivity(
        designs, pending, slot_map, reserved_by_edge, priority_by_design,
        edge_crs, work_crs, spacing,
    )
    if node_changes:
        _log(f"[node-exclusive] corrected ordinary Pole-node fan-in cases={node_changes}")

    def assign(edge_users, edge_reserved, previous_slots):
        counters["edges"] += 1
        counters["overlap_edges"] += int(len(edge_users) > 1 or bool(edge_reserved))
        assigned = {}
        for use in sorted(edge_users, key=lambda item: priority_by_design.get(item.design_index, (float("inf"), item.design_index))):
            key = (use.design_index, use.segment_index, use.edge_index)
            slot = int(slot_map.get(key, 0))
            use.slot = slot
            assigned[use.design_index] = slot
            counters["slots"][slot] = counters["slots"].get(slot, 0) + 1
        return assigned

    return assign, {"routes": routes, "ordered_designs": ordered_designs, "slot_map": slot_map}


def make_build_wrapper(original, control_distance_m=0.30):
    """Return the continuous lane builder; 0.30 m is compatibility-only."""
    def build(segment, slots_by_edge, spacing, work_crs, source_crs, edge_crs):
        return _v8.build_continuous_segment_points(segment, slots_by_edge, spacing, work_crs, source_crs, edge_crs)
    return build
