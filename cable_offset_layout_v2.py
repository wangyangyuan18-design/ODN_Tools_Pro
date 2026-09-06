# -*- coding: utf-8 -*-
"""Compatibility wrapper for the first Cable Offset Layout implementation.

This wrapper keeps the original geometry engine but fixes slot continuity:
when a Pole Edge has only one new cable and no existing cable occupies a
nearby slot, that cable returns to slot 0 instead of carrying an old offset
into a non-overlapping section.
"""

from . import cable_offset_layout as _base


def _assign_slots(edge_users, edge_reserved, previous_slots):
    assigned = {}
    used = set(edge_reserved or set())

    if len(edge_users) == 1 and not used:
        only = edge_users[0]
        only.slot = 0
        return {only.design_index: 0}

    users = sorted(
        edge_users,
        key=lambda item: (
            0 if previous_slots.get(item.design_index) is not None else 1,
            item.design_index,
            item.segment_index,
            item.edge_index,
        ),
    )

    for use in users:
        previous = previous_slots.get(use.design_index)
        if previous is not None and previous not in used:
            use.slot = previous
            used.add(previous)
            assigned[use.design_index] = previous

    for use in users:
        if use.slot is not None:
            continue
        for candidate in _base._slot_candidates(use.side_hint):
            if candidate not in used:
                use.slot = candidate
                used.add(candidate)
                assigned[use.design_index] = candidate
                break
        if use.slot is None:
            use.slot = 0
            assigned[use.design_index] = 0
    return assigned


_base._assign_slots = _assign_slots


def apply_layout_to_designs(designs, distribution_layer, edge_layer, spacing=0.5):
    return _base.apply_layout_to_designs(
        designs,
        distribution_layer,
        edge_layer,
        spacing=spacing,
    )
