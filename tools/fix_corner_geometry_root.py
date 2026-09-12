from pathlib import Path
import re

path = Path('cable_offset_core.py')
text = path.read_text(encoding='utf-8')
pattern = re.compile(r'def _same_lane_corner\(.*?\n(?=def _build_corner_geometry\()', re.S)
replacement = '''def _lane_offset_line(node, direction, slot, spacing):
    """Return the infinite cable lane line through the route corner.

    Lane is a lateral constraint.  It must not be represented by an arbitrary
    longitudinal run before/after the Pole.  This helper therefore constructs
    only the true offset line: route vertex + perpendicular Lane offset.
    """
    anchor = _offset_lane_point(node, direction, slot, spacing)
    return anchor, QgsPointXY(anchor.x() + direction[0], anchor.y() + direction[1])


def _unified_lane_corner(node, in_a, in_b, out_a, out_b, prev_slot, next_slot, spacing):
    """Build every ordinary corner from the two actual offset-lane lines.

    The previous D/E/F implementations used different artificial longitudinal
    distances (run_in, run_out, cross_run, turn_run, main_anchor, etc.).
    Those distances were the common source of the observed dog-leg/backtracking
    geometry, with the apparent excursion changing with Lane magnitude and
    edge length.

    The correct primitive is simpler: intersect the incoming and outgoing
    offset lane lines.  If they intersect at a reasonable distance, that is
    the corner.  If the lines are parallel or the intersection is numerically
    too far away, use a two-point bevel made only from the two lateral lane
    anchors.  No point is created by travelling backward or forward along an
    edge.
    """
    in_dir = _safe_unit(in_a, in_b)
    out_dir = _safe_unit(out_a, out_b)
    prev_slot = int(prev_slot)
    next_slot = int(next_slot)

    incoming_anchor, incoming_next = _lane_offset_line(
        node, in_dir, prev_slot, spacing
    )
    outgoing_anchor, outgoing_next = _lane_offset_line(
        node, out_dir, next_slot, spacing
    )

    # Main -> Main is the actual Pole ownership point.
    if prev_slot == 0 and next_slot == 0:
        return [QgsPointXY(node)]

    hit = _line_intersection(
        incoming_anchor, incoming_next,
        outgoing_anchor, outgoing_next,
    )

    # A real offset-line intersection is the cleanest corner.  Do not impose
    # a small arbitrary run; the distance is determined by the two lane lines.
    if hit is not None:
        distance = hypot(hit.x() - node.x(), hit.y() - node.y())
        max_reasonable = max(4.0 * float(spacing) * max(1, abs(prev_slot), abs(next_slot)), 2.0)
        if distance <= max_reasonable:
            return [hit]

    # Parallel / near-parallel / distant intersection: direct bevel between
    # the two true lane anchors.  These anchors differ only laterally from the
    # Pole, so this fallback cannot create a longitudinal dog-leg.
    if hypot(incoming_anchor.x() - outgoing_anchor.x(), incoming_anchor.y() - outgoing_anchor.y()) > 1e-7:
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


'''
new_text, count = pattern.subn(replacement, text, count=1)
if count != 1:
    raise SystemExit(f'corner root patch failed: matches={count}')
path.write_text(new_text, encoding='utf-8')
