# -*- coding: utf-8 -*-
"""Cable offset layout v3.

All offset/slot/transition calculations are performed in ONE projected CRS
whose map unit is metres.  Link route data remains stored in Pole Edge CRS;
the conversion back to Pole Edge CRS is performed once by the v2/base engine,
and link writing then converts Pole Edge CRS to Distribution Cable CRS once.
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


def _metric_crs(preferred, fallback):
    for crs in (preferred, fallback):
        try:
            if crs.isValid() and not crs.isGeographic() and crs.mapUnits() == QgsUnitTypes.DistanceMeters:
                return crs
        except Exception:
            pass
    source = fallback if fallback and fallback.isValid() else preferred
    if source and source.isValid():
        try:
            center = QgsPointXY(source.extent().center()) if hasattr(source, "extent") else None
        except Exception:
            center = None
    else:
        center = None
    return QgsCoordinateReferenceSystem("EPSG:3857")


def _choose_metric_work_crs(edge_layer, distribution_layer):
    """Prefer Distribution Cable CRS when it is projected metres.

    This makes the calculation CRS explicit and avoids ever doing metre-based
    offsets directly in EPSG:4326 degrees.
    """
    dc = distribution_layer.crs()
    edge = edge_layer.crs()
    try:
        if dc.isValid() and not dc.isGeographic() and dc.mapUnits() == QgsUnitTypes.DistanceMeters:
            return dc
    except Exception:
        pass
    try:
        if edge.isValid() and not edge.isGeographic() and edge.mapUnits() == QgsUnitTypes.DistanceMeters:
            return edge
    except Exception:
        pass
    # Let the existing base engine choose a local projected CRS when neither
    # layer is already metric.  It uses the Pole Edge extent to select UTM.
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
        return [base._transform_point(QgsPointXY(float(p[0]), float(p[1])), source_crs, work_crs) for p in stored if len(p) >= 2]

    stored_work = [base._transform_point(QgsPointXY(float(p[0]), float(p[1])), source_crs, work_crs) for p in stored if len(p) >= 2]
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
                _append(result, _offset(_point_at(pa, pb, max(0.0, plen - run)), pa, pb, prev_slot * spacing))
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
        _append(result, _offset(_point_at(a, b, max(0.0, length - run)), a, b, last_slot * spacing))
        _append(result, b)
    _append(result, end_point)
    return result


def _make_build_wrapper(original):
    def build(segment, slots_by_edge, spacing, work_crs, source_crs, edge_crs):
        try:
            return _build_explicit_points(segment, slots_by_edge, spacing, work_crs, source_crs, edge_crs)
        except Exception as exc:
            _log(f"[geometry-error] {segment.get('from', '?')}->{segment.get('to', '?')}; {type(exc).__name__}: {exc}", Qgis.Warning)
            return original(segment, slots_by_edge, spacing, work_crs, source_crs, edge_crs)
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
    suspicious = []
    transform = QgsCoordinateTransform(edge_crs, work_crs, QgsProject.instance().transformContext())
    for di, design in enumerate(designs or []):
        for si, segment in enumerate(design.get("segments", []) or []):
            points = segment.get("points", []) or []
            if len(points) < 2:
                continue
            try:
                geom = QgsGeometry.fromPolylineXY([transform.transform(QgsPointXY(float(p[0]), float(p[1]))) for p in points])
                length = float(geom.length())
                old = float(segment.get("pole_edge_distance", segment.get("distance", 0.0)) or 0.0)
                if length > 200000.0 or (old > 1.0 and length > old * 20.0):
                    suspicious.append((di, si, old, length))
            except Exception as exc:
                suspicious.append((di, si, -1.0, -1.0))
    if suspicious:
        for di, si, old, new in suspicious[:8]:
            _log(f"[ABORT] abnormal_geometry design={di}; segment={si}; old={old:.1f}m; new={new:.1f}m", Qgis.Critical)
        raise RuntimeError(f"检测到 {len(suspicious)} 个异常长度 Segment，已阻止写入。请检查 CRS/路径数据。")


def apply_explicit_layout_to_designs(designs, distribution_layer, edge_layer, spacing=0.5, angles=None):
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
    states = [(bool(d.get("written")), bool(d.get("needs_resync"))) for d in designs or []]

    try:
        _log("========== Offset START ==========")
        _log(f"[CRS] work={work_crs.authid()}; unit=m; edge={edge_crs.authid()}; DC={distribution_layer.crs().authid()}")
        _log(f"[input] links={len(designs or [])}; spacing={spacing:.2f}m; angles={sorted(allowed)}")

        segments = sum(len(d.get("segments", []) or []) for d in designs or [])
        edges = sum(len(s.get("edge_sequence", []) or []) for d in designs or [] for s in d.get("segments", []) or [])
        missing = sum(1 for d in designs or [] for s in d.get("segments", []) or [] if not (s.get("edge_sequence", []) or []))
        _log(f"[routes] segments={segments}; edges={edges}; missing_edge_sequence={missing}")

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
            summary = _v2.apply_layout_to_designs(designs, occupancy, edge_layer, spacing=spacing)
        finally:
            base._choose_work_crs = old["work_crs"]
            base._canonical_edge = old["canonical"]
            base._transition_angle = old["transition"]
            base._assign_slots = old["assign"]
            base._existing_slot_occupancy = old["occupancy"]
            base._build_segment_points = old["build"]

        _validate_result(designs, edge_crs, work_crs)
        nonzero = {k: v for k, v in counters["slots"].items() if k != 0}
        _log(f"[slots] edges={counters['edges']}; overlap_edges={counters['overlap_edges']}; nonzero={nonzero}")
        _log(f"[result] changed={summary.get('changed_designs', 0)}; extra={summary.get('extra_length_m', 0):.3f}m")
        _log("========== Offset END ==========")

        for d in designs or []:
            d["layout"] = {
                "version": 6,
                "spacing_m": round(spacing, 3),
                "angles_deg": [float(x) for x in sorted(allowed)],
                "rule": "local_overlap_parallel_transition",
                "work_crs": work_crs.authid(),
            }
            d["written"] = False
            d["needs_resync"] = True
        summary["angles_deg"] = [float(x) for x in sorted(allowed)]
        summary["work_crs"] = work_crs.authid()
        return summary
    except Exception as exc:
        _log(f"[ERROR] {type(exc).__name__}: {exc}", Qgis.Critical)
        for d, state in zip(designs or [], states):
            d["written"], d["needs_resync"] = state
        raise
