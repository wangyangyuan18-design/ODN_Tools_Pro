# -*- coding: utf-8 -*-
"""Explicit Pole Edge cable offset layout.

All distance/angle/offset calculations are performed in one projected CRS in
metres. The saved Link topology remains the authoritative Pole Edge
``edge_sequence`` and generated route geometry is converted back to Pole Edge
CRS before the normal Distribution Cable writer consumes it.
"""
from math import hypot, radians, tan

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsGeometry,
    QgsMessageLog,
    QgsPointXY,
    QgsProject,
    QgsUnitTypes,
    QgsVectorLayer,
    Qgis,
)

from . import cable_offset_layout_v2 as _v2

ALLOWED = (45.0, 60.0, 75.0, 90.0)
DEFAULT = (60.0, 75.0, 90.0)
LOG_TAG = "ODN_Tools_Pro / Cable Offset"


def _log(message, level=Qgis.Info):
    try:
        QgsMessageLog.logMessage(str(message), LOG_TAG, level)
    except Exception:
        pass


def _choose_metric_work_crs(edge_layer, distribution_layer):
    """Select a projected CRS whose map unit is metres."""
    dc = distribution_layer.crs()
    edge = edge_layer.crs()
    for crs in (dc, edge):
        try:
            if crs.isValid() and not crs.isGeographic() and crs.mapUnits() == QgsUnitTypes.DistanceMeters:
                return crs
        except Exception:
            pass
    candidate = _v2._base._choose_work_crs(edge_layer)
    try:
        if candidate.isValid() and not candidate.isGeographic() and candidate.mapUnits() == QgsUnitTypes.DistanceMeters:
            return candidate
    except Exception:
        pass
    return QgsCoordinateReferenceSystem("EPSG:3857")


def _manual_only_layer(distribution_layer, designs):
    layer = QgsVectorLayer(
        f"LineString?crs={distribution_layer.crs().authid()}",
        "ODN Offset Occupancy",
        "memory",
    )
    idx = distribution_layer.fields().indexOf("_ODN_LINK_ID")
    known = {str(d.get("_link_id")) for d in designs or [] if d.get("_link_id")}
    count = 0
    excluded = 0
    for feature in distribution_layer.getFeatures():
        if idx >= 0 and feature.attribute(idx) and str(feature.attribute(idx)) in known:
            excluded += 1
            continue
        clone = QgsFeature()
        clone.setGeometry(feature.geometry())
        layer.dataProvider().addFeature(clone)
        count += 1
    layer.updateExtents()
    _log(f"[occupancy] existing={count}; excluded_current={excluded}")
    return layer


def _transition_factory(angles):
    allowed = sorted(set(float(x) for x in (angles or DEFAULT) if float(x) in ALLOWED)) or list(DEFAULT)

    def transition(change):
        magnitude = abs(int(change))
        if len(allowed) == 1:
            return allowed[0]
        if magnitude <= 2:
            return allowed[0]
        if magnitude <= 4:
            return allowed[min(1, len(allowed) - 1)]
        return allowed[-1]

    return transition


def _make_geometry_only_canonical_edge(original):
    def canonical(raw):
        edge = original(raw)
        return None if edge is None else (0, edge[1], edge[2])
    return canonical


def _append(points, point, eps=1e-7):
    point = QgsPointXY(point)
    if not points or hypot(points[-1].x() - point.x(), points[-1].y() - point.y()) > eps:
        points.append(point)


def _point_at(a, b, distance):
    length = hypot(b.x() - a.x(), b.y() - a.y())
    if length <= 1e-12:
        return QgsPointXY(a)
    f = max(0.0, min(1.0, float(distance) / length))
    return QgsPointXY(a.x() + (b.x() - a.x()) * f, a.y() + (b.y() - a.y()) * f)


def _offset(point, a, b, distance):
    dx = b.x() - a.x()
    dy = b.y() - a.y()
    length = hypot(dx, dy)
    if length <= 1e-12 or abs(distance) <= 1e-12:
        return QgsPointXY(point)
    return QgsPointXY(point.x() - dy / length * distance, point.y() + dx / length * distance)


def _transition_run(offset_distance, angle_deg):
    if abs(offset_distance) <= 1e-12 or float(angle_deg) >= 89.5:
        return 0.0
    return abs(offset_distance) / tan(radians(float(angle_deg)))


def _build_explicit_points(segment, slots_by_edge, spacing, work_crs, source_crs, edge_crs):
    base = _v2._base
    raw_edges = segment.get("edge_sequence", []) or []
    edges = [e for raw in raw_edges if (e := base._canonical_edge(raw))]
    stored = segment.get("points", []) or []
    if not edges or len(stored) < 2:
        return [base._transform_point(QgsPointXY(float(p[0]), float(p[1])), source_crs, work_crs)
                for p in stored if len(p) >= 2]

    stored_work = [
        base._transform_point(QgsPointXY(float(p[0]), float(p[1])), source_crs, work_crs)
        for p in stored if len(p) >= 2
    ]
    if len(stored_work) < 2:
        return stored_work

    nodes = base._extract_route_graph_nodes(segment, work_crs, source_crs, edge_crs)
    if len(nodes) != len(edges) + 1:
        return stored_work

    slots = [int(slots_by_edge.get(i, 0)) for i in range(len(edges))]
    if not any(slots):
        return stored_work

    result = []
    start_point, end_point = stored_work[0], stored_work[-1]
    _append(result, start_point)

    for i, slot in enumerate(slots):
        a, b = nodes[i], nodes[i + 1]
        length = hypot(b.x() - a.x(), b.y() - a.y())
        if length <= 1e-12:
            continue

        prev_slot = slots[i - 1] if i else slot

        if i == 0:
            if slot == 0:
                _append(result, a)
            else:
                angle = base._transition_angle(slot)
                run = min(_transition_run(slot * spacing, angle), length * 0.45)
                _append(result, _offset(_point_at(a, b, run), a, b, slot * spacing))

        elif prev_slot != slot:
            if prev_slot == 0 and slot != 0:
                _append(result, a)
                angle = base._transition_angle(slot)
                run = min(_transition_run(slot * spacing, angle), length * 0.45)
                _append(result, _offset(_point_at(a, b, run), a, b, slot * spacing))

            elif prev_slot != 0 and slot == 0:
                pa, pb = nodes[i - 1], nodes[i]
                plen = hypot(pb.x() - pa.x(), pb.y() - pa.y())
                angle = base._transition_angle(prev_slot)
                run = min(_transition_run(prev_slot * spacing, angle), plen * 0.45)
                _append(
                    result,
                    _offset(_point_at(pa, pb, max(0.0, plen - run)), pa, pb, prev_slot * spacing),
                )
                _append(result, b)

            else:
                delta = (slot - prev_slot) * spacing
                angle = base._transition_angle(delta)
                run = min(_transition_run(delta, angle), length * 0.45)
                _append(result, _offset(a, a, b, prev_slot * spacing))
                _append(result, _offset(_point_at(a, b, run), a, b, slot * spacing))

        if slot == 0:
            _append(result, b)
        else:
            _append(result, _offset(b, a, b, slot * spacing))

    last_slot = slots[-1]
    if last_slot != 0:
        a, b = nodes[-2], nodes[-1]
        length = hypot(b.x() - a.x(), b.y() - a.y())
        angle = base._transition_angle(last_slot)
        run = min(_transition_run(last_slot * spacing, angle), length * 0.45)
        _append(
            result,
            _offset(_point_at(a, b, max(0.0, length - run)), a, b, last_slot * spacing),
        )
        _append(result, b)

    _append(result, end_point)
    return result


def _edge_nodes_for_segment(segment, work_crs, source_crs, edge_crs):
    base = _v2._base
    raw_edges = segment.get("edge_sequence", []) or []
    edges = [e for raw in raw_edges if (e := base._canonical_edge(raw))]
    nodes = base._extract_route_graph_nodes(segment, work_crs, source_crs, edge_crs)
    return edges, nodes


def _expected_route_length(segment, work_crs, source_crs, edge_crs):
    edges, nodes = _edge_nodes_for_segment(segment, work_crs, source_crs, edge_crs)
    if not edges or len(nodes) != len(edges) + 1:
        return None
    return sum(
        hypot(nodes[i + 1].x() - nodes[i].x(), nodes[i + 1].y() - nodes[i].y())
        for i in range(len(edges))
    )


def _transform_points(points, source_crs, target_crs):
    transform = QgsCoordinateTransform(source_crs, target_crs, QgsProject.instance().transformContext())
    return [transform.transform(QgsPointXY(float(p[0]), float(p[1]))) for p in points]


def _length_in_crs(points, source_crs, target_crs):
    try:
        transformed = _transform_points(points, source_crs, target_crs)
        return float(QgsGeometry.fromPolylineXY(transformed).length())
    except Exception:
        return None


def _direct_work_length(points):
    try:
        work_points = [QgsPointXY(float(p[0]), float(p[1])) for p in points]
        return float(QgsGeometry.fromPolylineXY(work_points).length())
    except Exception:
        return None


def _normalize_accidental_work_crs(designs, edge_crs, work_crs):
    """Repair generated points that accidentally remained in work CRS.

    The decision is evidence-based against the authoritative edge_sequence:
    declared-CRS interpretation, direct-work-CRS interpretation, and the
    reconstructed Pole Edge route length must agree before repair is applied.
    """
    repaired = 0
    for di, design in enumerate(designs or []):
        declared_crs = edge_crs
        for si, segment in enumerate(design.get("segments", []) or []):
            points = segment.get("points", []) or []
            raw_edges = segment.get("edge_sequence", []) or []
            if len(points) < 2 or not raw_edges:
                continue

            expected = _expected_route_length(segment, work_crs, declared_crs, edge_crs)
            if expected is None or expected <= 0:
                continue

            declared_len = _length_in_crs(points, declared_crs, work_crs)
            direct_work_len = _direct_work_length(points)
            if declared_len is None or direct_work_len is None:
                continue

            plausible_direct = (
                expected * 0.50 <= direct_work_len <= max(expected * 2.50, expected + 200.0)
            )
            declared_bad = (
                declared_len > 200000.0
                or declared_len > max(expected * 10.0, expected + 5000.0)
            )

            if declared_bad and plausible_direct:
                raw_work = [QgsPointXY(float(p[0]), float(p[1])) for p in points]
                repaired_edge = _transform_points(
                    [[p.x(), p.y()] for p in raw_work],
                    work_crs,
                    edge_crs,
                )
                segment["points"] = [[float(p.x()), float(p.y())] for p in repaired_edge]
                segment["source_crs"] = edge_crs.authid()
                repaired += 1
                _log(
                    f"[CRS-REPAIR] design={di}; segment={si}; "
                    f"source_before={declared_crs.authid()}; work={work_crs.authid()}; "
                    f"expected={expected:.3f}m; declared_length={declared_len:.3f}m; "
                    f"direct_work_length={direct_work_len:.3f}m; action=work_to_edge"
                )

    return repaired


def _sample_log(segment, slots_by_edge, spacing, work_crs, source_crs, edge_crs, output_points):
    """Print one complete offset case only, so the log remains usable."""
    base = _v2._base
    edges = [e for raw in (segment.get("edge_sequence", []) or []) if (e := base._canonical_edge(raw))]
    nodes = base._extract_route_graph_nodes(segment, work_crs, source_crs, edge_crs)
    slots = [int(slots_by_edge.get(i, 0)) for i in range(len(edges))]
    if not edges or len(nodes) != len(edges) + 1 or not any(slots):
        return False

    _log("[sample] BEGIN")
    _log(
        f"[sample] from={segment.get('from', '?')}; to={segment.get('to', '?')}; "
        f"edges={len(edges)}; output_points={len(output_points)}"
    )
    _log(
        f"[sample] source_crs={source_crs.authid()}; work_crs={work_crs.authid()}; "
        f"edge_crs={edge_crs.authid()}; spacing={spacing:.3f}m"
    )
    for i, edge in enumerate(edges):
        a, b = nodes[i], nodes[i + 1]
        length = hypot(b.x() - a.x(), b.y() - a.y())
        slot = slots[i]
        offset = slot * spacing
        angle = base._transition_angle(slot) if slot else 0.0
        run = _transition_run(offset, angle) if slot else 0.0
        _log(
            f"[sample-edge {i}] fid={edge[0]}; slot={slot}; offset={offset:+.3f}m; "
            f"length={length:.3f}m; angle={angle:.1f}deg; transition_run={run:.3f}m; "
            f"A=({a.x():.3f},{a.y():.3f}); B=({b.x():.3f},{b.y():.3f})"
        )

    out_geom = QgsGeometry.fromPolylineXY(output_points)
    expected = sum(
        hypot(nodes[i + 1].x() - nodes[i].x(), nodes[i + 1].y() - nodes[i].y())
        for i in range(len(edges))
    )
    _log(f"[sample-result] expected_edge_route={expected:.3f}m")
    _log(f"[sample-result] work_length={out_geom.length():.3f}m")
    _log(
        f"[sample-result] first_work=({output_points[0].x():.3f},{output_points[0].y():.3f}); "
        f"last_work=({output_points[-1].x():.3f},{output_points[-1].y():.3f})"
    )

    transform = QgsCoordinateTransform(
        work_crs, edge_crs, QgsProject.instance().transformContext()
    )
    edge_points = [transform.transform(p) for p in output_points]
    edge_geom = QgsGeometry.fromPolylineXY(edge_points)
    _log(
        f"[sample-result] edge_crs_length={edge_geom.length():.6f}; "
        f"first_edge=({edge_points[0].x():.8f},{edge_points[0].y():.8f}); "
        f"last_edge=({edge_points[-1].x():.8f},{edge_points[-1].y():.8f})"
    )
    _log("[sample] END")
    return True


def _make_build_wrapper(original):
    state = {"sample_logged": False}

    def build(segment, slots_by_edge, spacing, work_crs, source_crs, edge_crs):
        try:
            output = _build_explicit_points(
                segment, slots_by_edge, spacing, work_crs, source_crs, edge_crs
            )
            if not state["sample_logged"]:
                state["sample_logged"] = _sample_log(
                    segment, slots_by_edge, spacing,
                    work_crs, source_crs, edge_crs, output
                )
            return output
        except Exception as exc:
            _log(
                f"[geometry-error] {segment.get('from', '?')}->{segment.get('to', '?')}; "
                f"{type(exc).__name__}: {exc}",
                Qgis.Warning,
            )
            return original(
                segment, slots_by_edge, spacing, work_crs, source_crs, edge_crs
            )

    return build


def _make_slot_collector(original, counters):
    def assign(edge_users, edge_reserved, previous_slots):
        result = original(edge_users, edge_reserved, previous_slots)
        counters["edges"] += 1
        counters["overlap_edges"] += int(len(edge_users) > 1 or bool(edge_reserved))
        for value in result.values():
            value = int(value)
            counters["slots"][value] = counters["slots"].get(value, 0) + 1
        return result

    return assign


def _validate_result(designs, edge_crs, work_crs):
    """Validate topology, CRS consistency and final geometry length."""
    suspicious = []
    invalid_topology = []

    for di, design in enumerate(designs or []):
        source_crs = edge_crs
        for si, segment in enumerate(design.get("segments", []) or []):
            raw_edges = segment.get("edge_sequence", []) or []
            points = segment.get("points", []) or []

            if raw_edges and len(points) < 2:
                invalid_topology.append((di, si, "too_few_points"))
                continue

            edges, nodes = _edge_nodes_for_segment(
                segment, work_crs, source_crs, edge_crs
            )

            if raw_edges and (len(edges) != len(raw_edges) or len(nodes) != len(edges) + 1):
                invalid_topology.append((di, si, "edge_sequence_node_mismatch"))
                continue

            if len(points) < 2 or not edges:
                continue

            try:
                work_points = _transform_points(points, source_crs, work_crs)
                geom = QgsGeometry.fromPolylineXY(work_points)
                actual = float(geom.length())
                expected = _expected_route_length(
                    segment, work_crs, source_crs, edge_crs
                )
                direct_work = _direct_work_length(points)

                if expected is None or expected <= 0:
                    invalid_topology.append((di, si, "unable_to_measure_edge_sequence"))
                    continue

                ratio = actual / expected if expected > 1e-9 else float("inf")
                _log(
                    f"[validate] design={di}; segment={si}; "
                    f"edge_count={len(edges)}; expected_edge_route={expected:.3f}m; "
                    f"generated_work={actual:.3f}m; ratio={ratio:.3f}; "
                    f"direct_raw_as_work={(direct_work if direct_work is not None else -1.0):.3f}m"
                )

                if actual > 200000.0 or ratio > 10.0 or ratio < 0.10:
                    raw_first = points[0]
                    raw_last = points[-1]
                    _log(
                        f"[validate-detail] design={di}; segment={si}; "
                        f"raw_first=({float(raw_first[0]):.6f},{float(raw_first[1]):.6f}); "
                        f"raw_last=({float(raw_last[0]):.6f},{float(raw_last[1]):.6f}); "
                        f"source={source_crs.authid()}; work={work_crs.authid()}; "
                        f"expected={expected:.3f}m; generated={actual:.3f}m",
                        Qgis.Warning,
                    )
                    suspicious.append((di, si, expected, actual, ratio))

            except Exception as exc:
                _log(
                    f"[validate-error] design={di}; segment={si}; "
                    f"{type(exc).__name__}: {exc}",
                    Qgis.Critical,
                )
                suspicious.append((di, si, -1.0, -1.0, float("inf")))

    if invalid_topology:
        for item in invalid_topology[:8]:
            _log(
                f"[ABORT] topology_error design={item[0]}; segment={item[1]}; "
                f"reason={item[2]}",
                Qgis.Critical,
            )
        raise RuntimeError(
            f"检测到 {len(invalid_topology)} 个 Link Segment 拓扑数据异常，已阻止写入。"
        )

    if suspicious:
        for di, si, expected, actual, ratio in suspicious[:12]:
            _log(
                f"[ABORT] abnormal_geometry design={di}; segment={si}; "
                f"expected={expected:.1f}m; new={actual:.1f}m; ratio={ratio:.2f}x",
                Qgis.Critical,
            )
        raise RuntimeError(
            f"检测到 {len(suspicious)} 个异常长度 Segment，已阻止写入。"
        )


def apply_explicit_layout_to_designs(
    designs, distribution_layer, edge_layer, spacing=0.5, angles=None
):
    allowed = []
    for value in angles or DEFAULT:
        try:
            value = float(value)
            if value in ALLOWED and value not in allowed:
                allowed.append(value)
        except Exception:
            pass
    if not allowed:
        allowed = list(DEFAULT)

    try:
        spacing = max(0.01, float(spacing))
    except Exception:
        spacing = 0.5

    base = _v2._base
    edge_crs = edge_layer.crs()
    work_crs = _choose_metric_work_crs(edge_layer, distribution_layer)

    try:
        if work_crs.isGeographic() or work_crs.mapUnits() != QgsUnitTypes.DistanceMeters:
            raise RuntimeError(f"工作 CRS 不是米制投影：{work_crs.authid()}")
    except Exception:
        raise RuntimeError(f"无法建立米制工作 CRS：{work_crs.authid()}")

    old = {
        "transition": base._transition_angle,
        "canonical": base._canonical_edge,
        "assign": base._assign_slots,
        "occupancy": base._existing_slot_occupancy,
        "build": base._build_segment_points,
        "work_crs": base._choose_work_crs,
    }
    counters = {"edges": 0, "overlap_edges": 0, "slots": {}}
    states = [
        (bool(d.get("written")), bool(d.get("needs_resync")))
        for d in designs or []
    ]

    try:
        _log("========== Offset START ==========")
        _log(
            f"[CRS] work={work_crs.authid()}; unit=m; "
            f"edge={edge_crs.authid()}; DC={distribution_layer.crs().authid()}"
        )
        _log(
            f"[input] links={len(designs or [])}; spacing={spacing:.2f}m; "
            f"angles={sorted(allowed)}"
        )

        segments = sum(
            len(d.get("segments", []) or []) for d in designs or []
        )
        edges = sum(
            len(s.get("edge_sequence", []) or [])
            for d in designs or []
            for s in d.get("segments", []) or []
        )
        missing = sum(
            1
            for d in designs or []
            for s in d.get("segments", []) or []
            if not (s.get("edge_sequence", []) or [])
        )
        _log(
            f"[routes] segments={segments}; edges={edges}; "
            f"missing_edge_sequence={missing}"
        )

        for d in designs or []:
            d["written"] = False
            d["needs_resync"] = True

        occupancy = _manual_only_layer(distribution_layer, designs)

        base._choose_work_crs = lambda _edge_layer: work_crs
        base._canonical_edge = _make_geometry_only_canonical_edge(old["canonical"])
        base._transition_angle = _transition_factory(allowed)
        base._assign_slots = _make_slot_collector(old["assign"], counters)
        base._build_segment_points = _make_build_wrapper(old["build"])

        try:
            summary = _v2.apply_layout_to_designs(
                designs, occupancy, edge_layer, spacing=spacing
            )
        finally:
            base._choose_work_crs = old["work_crs"]
            base._canonical_edge = old["canonical"]
            base._transition_angle = old["transition"]
            base._assign_slots = old["assign"]
            base._existing_slot_occupancy = old["occupancy"]
            base._build_segment_points = old["build"]

        repaired = _normalize_accidental_work_crs(
            designs, edge_crs, work_crs
        )
        _log(f"[CRS-REPAIR] total={repaired}")

        _validate_result(designs, edge_crs, work_crs)

        nonzero = {k: v for k, v in counters["slots"].items() if k != 0}
        _log(
            f"[slots] edges={counters['edges']}; "
            f"overlap_edges={counters['overlap_edges']}; nonzero={nonzero}"
        )
        _log(
            f"[result] changed={summary.get('changed_designs', 0)}; "
            f"extra={summary.get('extra_length_m', 0):.3f}m"
        )
        _log("========== Offset END ==========")

        for d in designs or []:
            d["layout"] = {
                "version": 8,
                "spacing_m": round(spacing, 3),
                "angles_deg": [float(x) for x in sorted(allowed)],
                "rule": "local_overlap_parallel_transition",
                "work_crs": work_crs.authid(),
                "topology_source": "saved_edge_sequence",
                "length_validation": "edge_sequence_authoritative",
            }
            d["written"] = False
            d["needs_resync"] = True

        summary["angles_deg"] = [float(x) for x in sorted(allowed)]
        summary["work_crs"] = work_crs.authid()
        summary["length_validation"] = "edge_sequence_authoritative"
        summary["crs_repairs"] = repaired
        return summary

    except Exception as exc:
        _log(f"[ERROR] {type(exc).__name__}: {exc}", Qgis.Critical)
        for d, state in zip(designs or [], states):
            d["written"], d["needs_resync"] = state
        raise
