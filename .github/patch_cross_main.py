from pathlib import Path

path = Path('cable_offset_core.py')
text = path.read_text(encoding='utf-8')
old = '''def _cross_main_corner(node, in_a, in_b, out_a, out_b, prev_slot, next_slot, spacing):
    """E: cross Main Lane first, hold engineering spacing, then turn.

    The middle point is intentionally on the Main Lane route direction, but it
    is offset longitudinally from the Pole, so the cable never lands on the
    physical Pole node.  This gives the required "cross -> hold 0.50 m -> turn"
    behaviour when moving from +N to -M (or vice versa).
    """
    in_dir = _safe_unit(in_a, in_b)
    out_dir = _safe_unit(out_a, out_b)
    magnitude = max(abs(int(prev_slot)), abs(int(next_slot)), 1) * float(spacing)
    edge_in_length = hypot(in_b.x() - in_a.x(), in_b.y() - in_a.y())
    edge_out_length = hypot(out_b.x() - out_a.x(), out_b.y() - out_a.y())
    cross_run = _clamped_run(edge_in_length, 0.65 * magnitude)
    hold_run = _clamped_run(edge_out_length, 0.45 * float(spacing))
    turn_run = _clamped_run(edge_out_length, 0.90 * magnitude)

    incoming = _offset_lane_point(_point_back(node, in_dir, cross_run), in_dir, prev_slot, spacing)
    main_hold = _point_forward(node, out_dir, hold_run)
    target = _offset_lane_point(_point_forward(node, out_dir, turn_run), out_dir, next_slot, spacing)

    _log(
        f"[corner-debug] CROSS_MAIN; spacing={float(spacing):.3f}; "
        f"prev_slot={int(prev_slot)}; next_slot={int(next_slot)}; "
        f"magnitude={float(magnitude):.3f}; cross_run={float(cross_run):.3f}; "
        f"hold_run={float(hold_run):.3f}; turn_run={float(turn_run):.3f}; "
        f"node={_corner_debug_point(node)}; "
        f"incoming={_corner_debug_point(incoming)}; "
        f"main_hold={_corner_debug_point(main_hold)}; "
        f"target={_corner_debug_point(target)}"
    )

    return [incoming, main_hold, target]
'''
new = '''def _cross_main_corner(node, in_a, in_b, out_a, out_b, prev_slot, next_slot, spacing):
    """E: cross the Main Lane directly when the relative side changes.

    The previous implementation inserted a longitudinal ``main_hold`` point
    (0.45 * spacing = 0.225 m at the default 0.50 m spacing). That artificial
    control point created the observed dog-leg. CROSS_MAIN now transitions
    directly from the incoming offset lane to the outgoing target lane.
    """
    in_dir = _safe_unit(in_a, in_b)
    out_dir = _safe_unit(out_a, out_b)
    magnitude = max(abs(int(prev_slot)), abs(int(next_slot)), 1) * float(spacing)
    edge_in_length = hypot(in_b.x() - in_a.x(), in_b.y() - in_a.y())
    edge_out_length = hypot(out_b.x() - out_a.x(), out_b.y() - out_a.y())
    cross_run = _clamped_run(edge_in_length, 0.65 * magnitude)
    turn_run = _clamped_run(edge_out_length, 0.90 * magnitude)

    incoming = _offset_lane_point(_point_back(node, in_dir, cross_run), in_dir, prev_slot, spacing)
    target = _offset_lane_point(_point_forward(node, out_dir, turn_run), out_dir, next_slot, spacing)

    _log(
        f"[corner-debug] CROSS_MAIN; spacing={float(spacing):.3f}; "
        f"prev_slot={int(prev_slot)}; next_slot={int(next_slot)}; "
        f"magnitude={float(magnitude):.3f}; cross_run={float(cross_run):.3f}; "
        f"turn_run={float(turn_run):.3f}; hold_run=REMOVED; "
        f"node={_corner_debug_point(node)}; "
        f"incoming={_corner_debug_point(incoming)}; "
        f"target={_corner_debug_point(target)}"
    )

    return [incoming, target]
'''
if old not in text:
    raise SystemExit('Expected CROSS_MAIN implementation not found; no changes made.')
text = text.replace(old, new, 1)
path.write_text(text, encoding='utf-8')
print('CROSS_MAIN fix applied successfully.')
