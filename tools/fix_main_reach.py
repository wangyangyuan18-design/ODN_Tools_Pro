from pathlib import Path
import re

path = Path('cable_offset_core.py')
text = path.read_text(encoding='utf-8')
pattern = re.compile(r'def _main_reach_corner\(.*?\n(?=def _cross_main_corner\()', re.S)
replacement = '''def _main_reach_corner(node, in_a, in_b, out_a, out_b, prev_slot, next_slot, spacing):
    """F: leave Main Lane through the corner and enter the target lane directly.

    The old implementation inserted ``main_anchor`` behind the route vertex
    (0.45 * spacing = 0.225 m at the default spacing). For a Main Lane ->
    side Lane transition this created the observed backward-then-forward
    dog-leg. The corner must not travel backward after reaching the Pole.
    """
    out_dir = _safe_unit(out_a, out_b)
    d_next = abs(int(next_slot)) * float(spacing)
    run = max(float(spacing), d_next)
    edge_out_length = hypot(out_b.x() - out_a.x(), out_b.y() - out_a.y())
    run_out = _clamped_run(edge_out_length, 0.90 * run)

    target_after = _offset_lane_point(
        _point_forward(node, out_dir, run_out),
        out_dir,
        next_slot,
        spacing,
    )

    _log(
        f"[corner-debug] MAIN_REACH; spacing={float(spacing):.3f}; "
        f"prev_slot={int(prev_slot)}; next_slot={int(next_slot)}; "
        f"run_in=REMOVED; run_out={float(run_out):.3f}; "
        f"main_anchor=REMOVED; node={_corner_debug_point(node)}; "
        f"target_after={_corner_debug_point(target_after)}"
    )
    return [target_after]


'''
new_text, count = pattern.subn(replacement, text, count=1)
if count != 1:
    raise SystemExit(f'MAIN_REACH patch failed: matches={count}')
path.write_text(new_text, encoding='utf-8')
