# -*- coding: utf-8 -*-
"""Automatic parallel cable layout for overlapping Pole Edge routes.

Rules implemented here:
- Preserve non-overlapping routes at their original geometry.
- When multiple new links share the same Pole Edge segment, allocate parallel
  slots at a configurable spacing (default 0.5 m).
- Existing Distribution Cable geometry is treated as occupied and is never
  moved.
- Enter/exit transitions are diagonal and use 60/75/90 degree engineering
  style angles where geometry permits.
- Slot side is computed from the Pole Edge travel direction, so positive
  offset is always the left normal and negative offset is the right normal.
"""

from math import hypot, tan, radians

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsSpatialIndex,
    QgsUnitTypes,
)


DEFAULT_SPACING_M = 0.5
LAYOUT_VERSION = 1


def _node_tuple(raw):
    return (float(raw[0]), float(raw[1]))


def _canonical_edge(raw):
    """Normalize a stored JSON edge-id to (feature_id, node_a, node_b)."""
    if raw is None or len(raw) < 2:
        return None
    try:
        feature_id = int(raw[0])
        ends = raw[1]
        if len(ends) < 2:
            return None
        a = _node_tuple(ends[0])
        b = _node_tuple(ends[1])
        if a <= b:
            return feature_id, a, b
        return feature_id, b, a
    except (TypeError, ValueError, IndexError):
        return None


def _edge_points(edge):
    return QgsPointXY(edge[1][0], edge[1][1]), QgsPointXY(edge[2][0], edge[2][1])


def _unit(a, b):
    dx = float(b.x()) - float(a.x())
    dy = float(b.y()) - float(a.y())
    length = hypot(dx, dy)
    if length <= 1e-12:
        return 0.0, 0.0
    return dx / length, dy / length


def _left_normal(tangent):
    return -tangent[1], tangent[0]


def _cross(ax, ay, bx, by):
    return ax * by - ay * bx


def _offset_point(point, tangent, distance):
    nx, ny = _left_normal(tangent)
    return QgsPointXY(point.x() + nx * distance, point.y() + ny * distance)


def _point_along(a, b, fraction):
    fraction = max(0.0, min(1.0, float(fraction)))
    return QgsPointXY(
        a.x() + (b.x() - a.x()) * fraction,
        a.y() + (b.y() - a.y()) * fraction,
    )


def _same_point(a, b, eps=1e-8):
    return hypot(a.x() - b.x(), a.y() - b.y()) <= eps


def _append_point(points, point):
    point = QgsPointXY(point)
    if not points or not _same_point(points[-1], point):
        points.append(point)


def _point_distance(a, b):
    return hypot(a.x() - b.x(), a.y() - b.y())


def _shared_endpoint(a, b, c, d):
    if _same_point(a, c):
        return a
    if _same_point(a, d):
        return a
    if _same_point(b, c):
        return b
    if _same_point(b, d):
        return b
    return None


def _transition_angle(slot_change):
    """Engineering transition angle by slot magnitude.

    1st/2nd offset pair -> 60°
    3rd/4th pair -> 75°
    5th+ -> 90°
    """
    magnitude = abs(int(slot_change))
    if magnitude <= 2:
        return 60.0
    if magnitude <= 4:
        return 75.0
    return 90.0


def _transition_run(distance, angle_deg):
    if distance <= 1e-12:
        return 0.0
    if angle_deg >= 89.5:
        return 0.0
    value = tan(radians(float(angle_deg)))
    return distance / value if abs(value) > 1e-12 else distance


def _route_side_hint(start_point, end_point, edge_a, edge_b):
    """Return +1 for left, -1 for right, 0 when the endpoint is collinear."""
    tx, ty = _unit(edge_a, edge_b)
    vx = end_point.x() - edge_a.x()
    vy = end_point.y() - edge_a.y()
    signed = _cross(tx, ty, vx, vy)
    tolerance = max(1e-7, hypot(vx, vy) * 1e-7)
    if signed > tolerance:
        return 1
    if signed < -tolerance:
        return -1

    vx = start_point.x() - edge_a.x()
    vy = start_point.y() - edge_a.y()
    signed = _cross(tx, ty, vx, vy)
    if signed > tolerance:
        return 1
    if signed < -tolerance:
        return -1
    return 0


def _choose_work_crs(edge_layer):
    """Return a CRS whose coordinates are suitable for meter-based offsets."""
    source = edge_layer.crs()
    project = QgsProject.instance()

    try:
        if (
            source.isValid()
            and not source.isGeographic()
            and source.mapUnits() == QgsUnitTypes.DistanceMeters
        ):
            return source
    except Exception:
        pass

    if source.isGeographic():
        try:
            center = QgsPointXY(edge_layer.extent().center())
            to_wgs84 = QgsCoordinateTransform(
                source,
                QgsCoordinateReferenceSystem("EPSG:4326"),
                project.transformContext(),
            )
            center = to_wgs84.transform(center)
            zone = int((center.x() + 180.0) // 6.0) + 1
            zone = max(1, min(60, zone))
            epsg = (32600 if center.y() >= 0 else 32700) + zone
            candidate = QgsCoordinateReferenceSystem(f"EPSG:{epsg}")
            if candidate.isValid():
                return candidate
        except Exception:
            pass

    return QgsCoordinateReferenceSystem("EPSG:3857")


def _transform_geometry(geometry, source_crs, target_crs):
    geometry = QgsGeometry(geometry)
    if geometry.isEmpty() or source_crs == target_crs:
        return geometry
    transform = QgsCoordinateTransform(
        source_crs, target_crs, QgsProject.instance().transformContext()
    )
    geometry.transform(transform)
    return geometry


def _transform_point(point, source_crs, target_crs):
    point = QgsPointXY(point)
    if source_crs == target_crs:
        return point
    transform = QgsCoordinateTransform(
        source_crs, target_crs, QgsProject.instance().transformContext()
    )
    return transform.transform(point)


def _extract_route_graph_nodes(segment, work_crs, source_crs):
    """Reconstruct ordered graph nodes from the stored edge sequence."""
    raw_edges = segment.get("edge_sequence", []) or []
    edges = []
    for raw in raw_edges:
        parsed = _canonical_edge(raw)
        if parsed:
            edges.append(parsed)
    if not edges:
        return []

    stored = segment.get("points", []) or []
    stored_work = []
    for raw in stored:
        try:
            stored_work.append(
                _transform_point(
                    QgsPointXY(float(raw[0]), float(raw[1])),
                    source_crs,
                    work_crs,
                )
            )
        except Exception:
            continue

    first_a, first_b = _edge_points(edges[0])

    if len(edges) == 1:
        if len(stored_work) >= 2:
            target = stored_work[1]
            first = (
                first_a
                if _point_distance(target, first_a) <= _point_distance(target, first_b)
                else first_b
            )
            second = first_b if _same_point(first, first_a) else first_a
        else:
            first, second = first_a, first_b
        return [first, second]

    next_a, next_b = _edge_points(edges[1])
    shared = _shared_endpoint(first_a, first_b, next_a, next_b)
    if shared is not None:
        first = first_b if _same_point(first_a, shared) else first_a
    elif len(stored_work) >= 2:
        target = stored_work[1]
        first = (
            first_a
            if _point_distance(target, first_a) <= _point_distance(target, first_b)
            else first_b
        )
    else:
        first = first_a

    nodes = [first]
    current = first
    for idx, edge in enumerate(edges):
        a, b = _edge_points(edge)
        if not _same_point(current, a) and not _same_point(current, b):
            if idx + 1 < len(edges):
                na, nb = _edge_points(edges[idx + 1])
                shared_now = _shared_endpoint(a, b, na, nb)
                if shared_now is not None:
                    current = b if _same_point(a, shared_now) else a
                else:
                    current = a
            else:
                current = a
        next_node = b if _same_point(current, a) else a
        nodes.append(next_node)
        current = next_node
    return nodes


class _LayoutUse:
    __slots__ = (
        "design_index",
        "segment_index",
        "edge_index",
        "edge_key",
        "start_point",
        "end_point",
        "side_hint",
        "slot",
        "prev_edge_key",
    )

    def __init__(
        self,
        design_index,
        segment_index,
        edge_index,
        edge_key,
        start_point,
        end_point,
        side_hint,
    ):
        self.design_index = design_index
        self.segment_index = segment_index
        self.edge_index = edge_index
        self.edge_key = edge_key
        self.start_point = start_point
        self.end_point = end_point
        self.side_hint = side_hint
        self.slot = None
        self.prev_edge_key = None


def _build_existing_index(layer, work_crs):
    index = QgsSpatialIndex()
    geometries = {}
    for feature in layer.getFeatures():
        try:
            geom = _transform_geometry(feature.geometry(), layer.crs(), work_crs)
            if geom.isEmpty():
                continue
            temp = QgsFeature()
            temp.setId(int(feature.id()))
            temp.setGeometry(geom)
            index.addFeature(temp)
            geometries[int(feature.id())] = geom
        except Exception:
            continue
    return index, geometries


def _existing_slot_occupancy(edge_geom, spacing, index, geometries):
    """Return occupied integer slots near an atomic Pole Edge segment."""
    if edge_geom.isEmpty():
        return set()
    length = edge_geom.length()
    if length <= 1e-12:
        return set()

    midpoint = edge_geom.interpolate(length * 0.5).asPoint()
    corridor_distance = max(spacing * 2.2, 0.25)
    bbox = edge_geom.boundingBox()
    bbox = QgsRectangle(
        bbox.xMinimum() - corridor_distance,
        bbox.yMinimum() - corridor_distance,
        bbox.xMaximum() + corridor_distance,
        bbox.yMaximum() + corridor_distance,
    )
    reserved = set()
    try:
        candidate_ids = index.intersects(bbox)
    except Exception:
        candidate_ids = geometries.keys()

    corridor = edge_geom.buffer(corridor_distance, 4)
    tangent = _unit(
        QgsPointXY(edge_geom.vertexAt(0)),
        QgsPointXY(edge_geom.vertexAt(1)),
    )
    nx, ny = _left_normal(tangent)

    for fid in candidate_ids:
        geom = geometries.get(int(fid))
        if geom is None or geom.isEmpty():
            continue
        try:
            if geom.distance(edge_geom) > corridor_distance:
                continue
            overlap = corridor.intersection(geom)
            if overlap.isEmpty():
                continue
            if overlap.length() < max(length * 0.35, spacing * 0.4):
                continue
            nearest = geom.nearestPoint(QgsGeometry.fromPointXY(midpoint))
            if nearest.isEmpty():
                continue
            nearest_point = nearest.asPoint()
            signed = (
                (nearest_point.x() - midpoint.x()) * nx
                + (nearest_point.y() - midpoint.y()) * ny
            )
            reserved.add(int(round(signed / spacing)))
        except Exception:
            continue
    return reserved


def _slot_candidates(side_hint, limit=20):
    yield 0
    for magnitude in range(1, limit + 1):
        if side_hint >= 0:
            yield magnitude
            yield -magnitude
        else:
            yield -magnitude
            yield magnitude


def _assign_slots(edge_users, edge_reserved, previous_slots):
    """Allocate slots while preserving route continuity and existing cables."""
    assigned = {}
    used = set(edge_reserved or set())
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
        for candidate in _slot_candidates(use.side_hint):
            if candidate not in used:
                use.slot = candidate
                used.add(candidate)
                assigned[use.design_index] = candidate
                break
        if use.slot is None:
            use.slot = 0
            assigned[use.design_index] = 0
    return assigned


def _build_segment_points(segment, slots_by_edge, spacing, work_crs, source_crs):
    raw_edges = segment.get("edge_sequence", []) or []
    edges = []
    for raw in raw_edges:
        parsed = _canonical_edge(raw)
        if parsed:
            edges.append(parsed)
    stored = segment.get("points", []) or []
    if not edges or len(stored) < 2:
        return [
            _transform_point(
                QgsPointXY(float(raw[0]), float(raw[1])),
                source_crs,
                work_crs,
            )
            for raw in stored
            if len(raw) >= 2
        ]

    work_stored = [
        _transform_point(
            QgsPointXY(float(raw[0]), float(raw[1])),
            source_crs,
            work_crs,
        )
        for raw in stored
        if len(raw) >= 2
    ]
    if len(work_stored) < 2:
        return work_stored

    nodes = _extract_route_graph_nodes(segment, work_crs, source_crs)
    if len(nodes) != len(edges) + 1:
        return work_stored

    slots = [int(slots_by_edge.get(i, 0)) for i in range(len(edges))]
    if not any(slot != 0 for slot in slots):
        return work_stored

    result = []
    start_point = work_stored[0]
    end_point = work_stored[-1]

    first_edge_len = _point_distance(nodes[0], nodes[1])
    first_slot = slots[0]
    if first_slot == 0:
        _append_point(result, start_point)
        _append_point(result, nodes[0])
    else:
        _append_point(result, start_point)
        angle = _transition_angle(first_slot)
        offset_distance = abs(first_slot) * spacing
        run = min(_transition_run(offset_distance, angle), first_edge_len * 0.45)
        fraction = run / first_edge_len if first_edge_len > 1e-12 else 0.0
        target_center = _point_along(nodes[0], nodes[1], fraction)
        target_offset = _offset_point(
            target_center,
            _unit(nodes[0], nodes[1]),
            first_slot * spacing,
        )
        _append_point(result, target_offset)

    for i in range(len(edges)):
        a = nodes[i]
        b = nodes[i + 1]
        t = _unit(a, b)
        slot = slots[i]
        offset_b = _offset_point(b, t, slot * spacing)

        if i > 0:
            prev_slot = slots[i - 1]
            prev_a = nodes[i - 1]
            prev_b = nodes[i]
            prev_t = _unit(prev_a, prev_b)
            prev_offset_b = _offset_point(prev_b, prev_t, prev_slot * spacing)

            if prev_slot == slot:
                _append_point(result, _offset_point(a, t, slot * spacing))
            elif prev_slot == 0 and slot != 0:
                _append_point(result, nodes[i])
                delta = abs(slot - prev_slot) * spacing
                angle = _transition_angle(slot - prev_slot)
                edge_len = _point_distance(a, b)
                run = min(_transition_run(delta, angle), edge_len * 0.45)
                fraction = run / edge_len if edge_len > 1e-12 else 0.0
                p = _point_along(a, b, fraction)
                _append_point(result, _offset_point(p, t, slot * spacing))
            elif prev_slot != 0 and slot == 0:
                delta = abs(prev_slot - slot) * spacing
                angle = _transition_angle(prev_slot - slot)
                prev_len = _point_distance(prev_a, prev_b)
                run = min(_transition_run(delta, angle), prev_len * 0.45)
                fraction = 1.0 - run / prev_len if prev_len > 1e-12 else 1.0
                p = _point_along(prev_a, prev_b, fraction)
                p = _offset_point(p, prev_t, prev_slot * spacing)
                if result:
                    result[-1] = p
                else:
                    _append_point(result, p)
                _append_point(result, nodes[i])
            else:
                delta = abs(prev_slot - slot) * spacing
                angle = _transition_angle(prev_slot - slot)
                prev_len = _point_distance(prev_a, prev_b)
                next_len = _point_distance(a, b)
                run_prev = min(_transition_run(delta, angle), prev_len * 0.35)
                run_next = min(_transition_run(delta, angle), next_len * 0.35)
                frac_prev = 1.0 - run_prev / prev_len if prev_len > 1e-12 else 1.0
                frac_next = run_next / next_len if next_len > 1e-12 else 0.0
                p1 = _offset_point(
                    _point_along(prev_a, prev_b, frac_prev),
                    prev_t,
                    prev_slot * spacing,
                )
                p2 = _offset_point(
                    _point_along(a, b, frac_next),
                    t,
                    slot * spacing,
                )
                if result:
                    result[-1] = p1
                else:
                    _append_point(result, p1)
                _append_point(result, p2)

        if i < len(edges) - 1:
            _append_point(result, offset_b)
        else:
            if slot == 0:
                _append_point(result, b)
                _append_point(result, end_point)
            else:
                final_len = _point_distance(a, b)
                angle = _transition_angle(slot)
                run = min(_transition_run(abs(slot) * spacing, angle), final_len * 0.45)
                fraction = 1.0 - run / final_len if final_len > 1e-12 else 0.5
                exit_center = _point_along(a, b, fraction)
                exit_point = _offset_point(exit_center, t, slot * spacing)
                _append_point(result, exit_point)
                _append_point(result, end_point)

    return result


def apply_layout_to_designs(
    designs,
    distribution_layer,
    edge_layer,
    spacing=DEFAULT_SPACING_M,
):
    """Mutate pending design geometries in-place and return a summary."""
    try:
        spacing = float(spacing)
    except (TypeError, ValueError):
        spacing = DEFAULT_SPACING_M
    if spacing <= 0:
        spacing = DEFAULT_SPACING_M

    work_crs = _choose_work_crs(edge_layer)
    existing_index, existing_geometries = _build_existing_index(
        distribution_layer,
        work_crs,
    )

    pending = []
    for design_index, design in enumerate(designs or []):
        if design.get("written") and not design.get("needs_resync"):
            continue
        segments = design.get("segments", []) or []
        for segment_index, segment in enumerate(segments):
            source_crs = edge_layer.crs()
            authid = str(design.get("source_crs", "") or "")
            if authid:
                candidate = QgsCoordinateReferenceSystem(authid)
                if candidate.isValid():
                    source_crs = candidate
            raw_edges = segment.get("edge_sequence", []) or []
            parsed_edges = []
            for raw in raw_edges:
                parsed = _canonical_edge(raw)
                if parsed:
                    parsed_edges.append(parsed)
            points = segment.get("points", []) or []
            if not parsed_edges or len(points) < 2:
                continue

            start_point = _transform_point(
                QgsPointXY(float(points[0][0]), float(points[0][1])),
                source_crs,
                work_crs,
            )
            end_point = _transform_point(
                QgsPointXY(float(points[-1][0]), float(points[-1][1])),
                source_crs,
                work_crs,
            )
            nodes = _extract_route_graph_nodes(segment, work_crs, source_crs)
            if len(nodes) != len(parsed_edges) + 1:
                continue

            for edge_index, edge in enumerate(parsed_edges):
                a = _transform_point(
                    QgsPointXY(edge[1][0], edge[1][1]),
                    source_crs,
                    work_crs,
                )
                b = _transform_point(
                    QgsPointXY(edge[2][0], edge[2][1]),
                    source_crs,
                    work_crs,
                )
                hint = _route_side_hint(start_point, end_point, a, b)
                use = _LayoutUse(
                    design_index,
                    segment_index,
                    edge_index,
                    edge,
                    start_point,
                    end_point,
                    hint,
                )
                if edge_index > 0:
                    use.prev_edge_key = parsed_edges[edge_index - 1]
                pending.append(use)

    users_by_edge = {}
    for use in pending:
        users_by_edge.setdefault(use.edge_key, []).append(use)

    edge_priority = {}
    for use in pending:
        position = (use.design_index, use.segment_index, use.edge_index)
        current = edge_priority.get(use.edge_key)
        if current is None or position < current:
            edge_priority[use.edge_key] = position

    slots_by_use = {}
    previous_slots = {}

    for edge_key in sorted(users_by_edge, key=lambda key: edge_priority[key]):
        users = users_by_edge[edge_key]
        a, b = _edge_points(edge_key)
        a = _transform_point(a, edge_layer.crs(), work_crs)
        b = _transform_point(b, edge_layer.crs(), work_crs)
        edge_geom = QgsGeometry.fromPolylineXY([a, b])
        reserved = _existing_slot_occupancy(
            edge_geom,
            spacing,
            existing_index,
            existing_geometries,
        )
        assigned = _assign_slots(users, reserved, previous_slots)
        for use in users:
            use.slot = assigned[use.design_index]
            slots_by_use[(use.design_index, use.segment_index, use.edge_index)] = use.slot

        for use in users:
            previous_slots[use.design_index] = use.slot

    changed_designs = set()
    total_extra_m = 0.0

    for design_index, design in enumerate(designs or []):
        if design.get("written") and not design.get("needs_resync"):
            continue
        segments = design.get("segments", []) or []
        source_crs = edge_layer.crs()
        authid = str(design.get("source_crs", "") or "")
        if authid:
            candidate = QgsCoordinateReferenceSystem(authid)
            if candidate.isValid():
                source_crs = candidate

        new_segments = []
        design_length = 0.0
        changed = False

        for segment_index, segment in enumerate(segments):
            segment_slots = {}
            for edge_index, raw in enumerate(segment.get("edge_sequence", []) or []):
                if _canonical_edge(raw) is not None:
                    segment_slots[edge_index] = slots_by_use.get(
                        (design_index, segment_index, edge_index),
                        0,
                    )

            new_work_points = _build_segment_points(
                segment,
                segment_slots,
                spacing,
                work_crs,
                source_crs,
            )
            old_work_points = [
                _transform_point(
                    QgsPointXY(float(raw[0]), float(raw[1])),
                    source_crs,
                    work_crs,
                )
                for raw in segment.get("points", [])
                if len(raw) >= 2
            ]

            if len(new_work_points) >= 2:
                if len(new_work_points) != len(old_work_points) or any(
                    not _same_point(a, b, 1e-7)
                    for a, b in zip(new_work_points, old_work_points)
                ):
                    changed = True

                out_points = [
                    _transform_point(p, work_crs, source_crs)
                    for p in new_work_points
                ]
                segment_copy = dict(segment)
                segment_copy["points"] = [
                    [float(p.x()), float(p.y())] for p in out_points
                ]
                work_geom = QgsGeometry.fromPolylineXY(new_work_points)
                segment_copy["distance"] = round(float(work_geom.length()), 3)
                segment_copy["layout_spacing"] = round(spacing, 3)
                new_segments.append(segment_copy)
                design_length += float(work_geom.length())

                old_geom = QgsGeometry.fromPolylineXY(old_work_points)
                if work_geom.length() > old_geom.length():
                    total_extra_m += work_geom.length() - old_geom.length()
            else:
                new_segments.append(segment)

        if changed:
            design["segments"] = new_segments
            design["length"] = round(design_length, 3)
            design["layout"] = {
                "version": LAYOUT_VERSION,
                "spacing_m": round(spacing, 3),
                "rules": [
                    "avoid_overlapping_new_cables",
                    "existing_cable_priority",
                    "automatic_slot_allocation",
                ],
            }
            changed_designs.add(design_index)

    for design in designs or []:
        if not design.get("source_crs"):
            design["source_crs"] = edge_layer.crs().authid()

    return {
        "changed_designs": len(changed_designs),
        "changed_indices": sorted(changed_designs),
        "spacing_m": spacing,
        "extra_length_m": round(total_extra_m, 3),
        "version": LAYOUT_VERSION,
    }
