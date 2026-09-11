# -*- coding: utf-8 -*-
"""ODN 2.x lane allocator / endpoint geometry v10.

Problem 1-8 consolidation:
- main lane (slot 0) is preferred whenever it is actually available;
- a route keeps its previous relative lane across consecutive edges;
- a new route joins an existing cable group from the outside;
- ordinary Pole nodes are exclusive;
- ordinary turns do not use the 0.30 m takeoff distance;
- only explicit special endpoints (FDT/FAT-return/BB/SFC-CL/Closure) use
  the 0.30 m takeoff geometry;
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

    # New route: true main lane first, then the entering side outward.
    if 0 not in used and 0 not in reserved:
        yield 0
    sign = 1 if use.side_hint >= 0 else -1
    for magnitude in range(1, limit + 1):
        yield sign * magnitude
    for magnitude in range(1, limit + 1):
        yield -sign * magnitude


def _mark_endpoint_modes(designs):
    """Attach explicit endpoint semantics to each segment.

    sequence_ids belongs to the Design, not normally to an individual segment.
    Copying only the two endpoint flags into the segment avoids guessing from
    ``slot != 0`` inside the geometry builder.
    """
    for design in designs or []:
        ids = design.get("sequence_ids", []) or []
        for index, segment in enumerate(design.get("segments", []) or []):
            start_item = ids[index] if index < len(ids) else None
            end_item = ids[index + 1] if index + 1 < len(ids) else None
            segment["_odn_special_start"] = bool(_v9._shared_node_exception(start_item))
            segment["_odn_special_end"] = bool(_v9._shared_node_exception(end_item))


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

    # Route-sequential allocation fixes the old edge-first problem: the main
    # route receives slot 0 when available and its previous lane is known when
    # the next Pole Edge is processed.
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

            # A new segment is a real route discontinuity; otherwise inherit
            # the preceding edge's relative lane.
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

    node_changes = _v9._enforce_ordinary_node_exclusivity(
        designs, pending, slot_map, reserved_by_edge, priority_by_design,
        edge_crs, work_crs, spacing,
    )
    if node_changes:
        _v9._log(f"[node-exclusive-v10] corrected ordinary Pole-node cases={node_changes}")

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
        "route_sequential_allocation=ON; endpoint_modes=explicit"
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
    """Make ordinary endpoint entry straight and reserve takeoff for specials."""
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
            # NORMAL_ENDPOINT: cable goes directly to the Pole. Do not invent
            # a 0.30 m diagonal merely because the lane is non-zero.
            return _v8.QgsPointXY(a)
        return original(nodes, slots, i, spacing)

    return entry


def _patch_endpoint_exit(segment):
    """Make the final endpoint land directly on the destination Pole/node."""
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
