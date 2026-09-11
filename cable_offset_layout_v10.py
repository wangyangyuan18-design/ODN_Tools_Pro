# -*- coding: utf-8 -*-
"""ODN 2.x lane allocator / endpoint geometry v10.

Problem 1-8 consolidation:
- main lane (slot 0) is preferred whenever it is actually available;
- a route keeps its previous relative lane across consecutive edges;
- a new route joins an existing cable group from the outside;
- ordinary Pole nodes are exclusive;
- ordinary turns do not use the 0.30 m takeoff distance;
- only explicit special endpoints use the 0.30 m takeoff geometry;
- FAT is exclusive between independent Links, while FAT Return on the same
  Link may use the same engineering point;
- FAT geometry is handled by the final cable geometry of its owning Link.
"""

from . import cable_offset_layout_v9 as _v9
from . import cable_offset_layout_v8 as _v8


def _lane_candidates(use, previous_slot, used, reserved, limit=100):
    """Stable lane continuity with main-lane preference for new routes."""
    if previous_slot is not None:
        previous_slot = int(previous_slot)
        yield previous_slot
        if previous_slot == 0:
            sign = 1 if use.side_hint >= 0 else -1
            for magnitude in range(1, limit + 1):
                yield sign * magnitude
            for magnitude in range(1, limit + 1):
                yield -sign * magnitude
        else:
            sign = 1 if previous_slot > 0 else -1
            for magnitude in range(abs(previous_slot) + 1, limit + 1):
                yield sign * magnitude
            for magnitude in range(1, limit + 1):
                yield -sign * magnitude
        return

    if 0 not in used and 0 not in reserved:
        yield 0
    sign = 1 if use.side_hint >= 0 else -1
    for magnitude in range(1, limit + 1):
        yield sign * magnitude
    for magnitude in range(1, limit + 1):
        yield -sign * magnitude


def _node_type(item):
    if not item or len(item) < 1:
        return ""
    text = str(item[0]).strip().upper()
    return "".join(ch for ch in text if ch.isalnum())


def _endpoint_is_special(item):
    """Special takeoff whitelist, including FAT for same-Link Return.

    FAT is intentionally special for endpoint geometry but is NOT treated as
    globally shareable by ordinary-node exclusivity. Two independent Links
    cannot use the same FAT.
    """
    kind = _node_type(item)
    if kind == "FAT":
        return True
    return _v9._shared_node_exception(item)


def _mark_endpoint_modes(designs):
    ids_by_design = {}
    for design in designs or []:
        ids = design.get("sequence_ids", []) or []
        ids_by_design[id(design)] = ids
        for index, segment in enumerate(design.get("segments", []) or []):
            start_item = ids[index] if index < len(ids) else None
            end_item = ids[index + 1] if index + 1 < len(ids) else None
            segment["_odn_special_start"] = _endpoint_is_special(start_item)
            segment["_odn_special_end"] = _endpoint_is_special(end_item)


def _fat_nodes_are_link_exclusive(designs, pending, slot_map, reserved_by_edge, priority_by_design, edge_crs, work_crs, spacing):
    """Run v9 node protection with FAT treated as non-shareable across Links.

    v9's generic exception list contains FAT because FAT Return may legitimately
    have two cables on one Link. For problem 5, however, that exception must
    not allow two independent Link designs to use the same FAT. This helper
    temporarily changes only the node-classification function used by the
    exclusivity pass.
    """
    original = _v9._shared_node_exception

    def strict_shared(item):
        return _endpoint_is_special(item) and _node_type(item) != "FAT"

    _v9._shared_node_exception = strict_shared
    try:
        return _v9._enforce_ordinary_node_exclusivity(
            designs, pending, slot_map, reserved_by_edge, priority_by_design,
            edge_crs, work_crs, spacing,
        )
    finally:
        _v9._shared_node_exception = original


def make_global_slot_assigner(designs, distribution_layer, edge_layer, spacing, counters):
    """Allocate lanes in complete-route order instead of independent edges."""
    base = _v9._base
    edge_crs = edge_layer.crs()
    work_crs = base._choose_work_crs(edge_layer)
    _mark_endpoint_modes(designs)
    pending, routes = _v9._collect_uses(designs, edge_crs, work_crs)

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
    existing_index, existing_geometries = base._build_existing_index(distribution_layer, work_crs)
    reserved_cache = {}
    reserved_by_edge = {}
    slot_map = {}
    used_by_edge = {}

    def reserved_for(edge_key):
        if edge_key not in reserved_by_edge:
            reserved_by_edge[edge_key] = set(
                _v9._reserved_for_edge(
                    edge_key, edge_crs, work_crs, spacing,
                    existing_index, existing_geometries, reserved_cache,
                )
            )
        return reserved_by_edge[edge_key]

    for design_index in ordered_designs:
        route_uses = sorted(
            [u for u in pending if u.design_index == design_index],
            key=lambda u: (u.segment_index, u.edge_index),
        )
        previous_slot = None
        previous_segment = None
        for use in route_uses:
            edge_key = use.edge_key
            reserved = reserved_for(edge_key)
            used = used_by_edge.setdefault(edge_key, set(reserved))
            if previous_segment is not None and use.segment_index != previous_segment:
                previous_slot = None

            chosen = None
            for candidate in _lane_candidates(use, previous_slot, used, reserved):
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

            key = (use.design_index, use.segment_index, use.edge_index)
            slot_map[key] = chosen
            use.slot = chosen
            used.add(chosen)
            previous_slot = chosen
            previous_segment = use.segment_index

    node_changes = _fat_nodes_are_link_exclusive(
        designs, pending, slot_map, reserved_by_edge, priority_by_design,
        edge_crs, work_crs, spacing,
    )
    if node_changes:
        _v9._log(f"[node-exclusive-v10] corrected ordinary/FAT cross-Link cases={node_changes}")

    def assign(edge_users, edge_reserved, previous_slots):
        counters["edges"] += 1
        counters["overlap_edges"] += int(len(edge_users) > 1 or bool(edge_reserved))
        assigned = {}
        for use in sorted(
            edge_users,
            key=lambda item: priority_by_design.get(
                item.design_index, (float("inf"), item.design_index)
            ),
        ):
            key = (use.design_index, use.segment_index, use.edge_index)
            slot = int(slot_map.get(key, 0))
            use.slot = slot
            assigned[use.design_index] = slot
            counters["slots"][slot] = counters["slots"].get(slot, 0) + 1
        return assigned

    _v9._log(
        f"[global-lane-v10] links={len(ordered_designs)}; "
        f"mapped_edges={len(slot_map)}; main_lane=preferred; "
        "route_sequential_allocation=ON; endpoint_modes=explicit; "
        "fat_cross_link_exclusive=ON"
    )
    return assign, {
        "routes": routes,
        "ordered_designs": ordered_designs,
        "slot_map": slot_map,
        "allocator_version": "v10",
    }


def _is_special_endpoint(segment, at_start):
    return bool(segment.get("_odn_special_start" if at_start else "_odn_special_end", False))


def _patch_endpoint_entry(segment):
    """Ordinary endpoint -> direct; special endpoint -> 0.30 m takeoff."""
    original = _v8._entry_point
    special_start = _is_special_endpoint(segment, True)

    def entry(nodes, slots, i, spacing):
        if i == 0:
            a, b = nodes[i], nodes[i + 1]
            slot = slots[i]
            if slot == 0:
                return _v8.QgsPointXY(a)
            if special_start:
                return _v8._takeoff_entry(a, b, slot, spacing)
            return _v8.QgsPointXY(a)
        return original(nodes, slots, i, spacing)

    return entry


def _patch_endpoint_exit(segment):
    """All final endpoint arrivals land directly on the destination node."""
    original = _v8._exit_point

    def exit_point(nodes, slots, i, spacing):
        if i == len(slots) - 1:
            return _v8.QgsPointXY(nodes[i + 1])
        return original(nodes, slots, i, spacing)

    return exit_point


def make_build_wrapper(original, control_distance_m=0.30):
    """Use v8 continuous geometry with explicit endpoint semantics."""
    def build(segment, slots_by_edge, spacing, work_crs, source_crs, edge_crs):
        original_entry = _v8._entry_point
        original_exit = _v8._exit_point
        _v8._entry_point = _patch_endpoint_entry(segment)
        _v8._exit_point = _patch_endpoint_exit(segment)
        try:
            return _v8.build_continuous_segment_points(
                segment, slots_by_edge, spacing, work_crs, source_crs, edge_crs
            )
        finally:
            _v8._entry_point = original_entry
            _v8._exit_point = original_exit
    return build
