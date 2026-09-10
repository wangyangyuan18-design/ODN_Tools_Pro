# -*- coding: utf-8 -*-
"""Lightweight ODN 2.1 constraint planning.

ODN 2.1 keeps the existing Pole Edge route engine.  This module only adds the
engineering constraints for BB and SFC Closure and deliberately keeps the
Link Design UI unchanged.

Phase 1 is intentionally conservative: it analyses the authoritative Pole
Edge route, records detailed diagnostics, and exposes candidate node positions.
It does not replace the existing Link/offset geometry engine.
"""

from collections import defaultdict

from qgis.core import QgsFeature, QgsGeometry, QgsPointXY, QgsMessageLog, Qgis


_LOG_TAG = "ODN_Tools_Pro / ODN 2.1"


def _log(message, level=Qgis.Info):
    try:
        QgsMessageLog.logMessage(str(message), _LOG_TAG, level)
    except Exception:
        pass


def _parameters(payload):
    return (payload or {}).get("parameters") or {}


def _number(payload, names, default):
    params = _parameters(payload)
    for name in names:
        value = params.get(name)
        if value in (None, ""):
            continue
        try:
            value = float(value)
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    return float(default)


def enabled(payload, role):
    """Best-effort read of the existing project configuration; no UI changes."""
    data = payload or {}
    version = str((data.get("project") or {}).get("odn_version", "")).strip().lower()
    if version not in ("2.1", "odn 2.1", "odn2.1", "odn_2.1"):
        return False
    registry = data.get("layer_registry") or {}
    # A bound BB/SFC layer is the strongest indication that the existing
    # project configuration enabled that node type.
    if role in registry and registry.get(role, {}).get("layer_id"):
        return True
    for key in ("node_types", "enabled_nodes", "access_node_types", "node_type"):
        values = data.get(key)
        if isinstance(values, dict):
            value = values.get(role)
            if value is True:
                return True
        elif isinstance(values, (list, tuple, set)) and role in values:
            return True
    return False


def is_odn21(payload):
    version = str(((payload or {}).get("project") or {}).get("odn_version", "")).strip().lower()
    return version in ("2.1", "odn 2.1", "odn2.1", "odn_2.1")


def limits(payload):
    return {
        "return_cable": _number(
            payload,
            ("return_cable_max_distance", "return_cable_limit", "back_cable_max_distance", "back_cable_limit"),
            100.0,
        ),
        "bb_trigger": _number(
            payload,
            ("bb_trigger_distance", "bb_return_cable_trigger", "bb_limit"),
            150.0,
        ),
        "prelinked": _number(
            payload,
            ("prelinked_cable_max_length", "pre_linked_cable_max_length", "dc_cable_max_length", "optical_cable_max_length"),
            455.0,
        ),
    }


def _edge_key(edge):
    try:
        return edge
    except Exception:
        return str(edge)


def _polyline_from_points(points):
    if not points or len(points) < 2:
        return None
    try:
        return QgsGeometry.fromPolylineXY([QgsPointXY(float(p[0]), float(p[1])) for p in points])
    except Exception:
        return None


def _split_at_graph_node(segment, graph_nodes, graph_points, node_index):
    """Split a route using the exact graph node point when possible."""
    raw = segment.get("points") or []
    if node_index < 0 or node_index >= len(graph_nodes) or not raw:
        return None
    target = graph_points[node_index]
    tx, ty = float(target.x()), float(target.y())
    pivot = None
    for i, p in enumerate(raw):
        if abs(float(p[0]) - tx) <= 1e-7 and abs(float(p[1]) - ty) <= 1e-7:
            pivot = i
            break
    if pivot is None:
        return None
    return raw[: pivot + 1], raw[pivot:]


def _find_return_loops(segment):
    nodes = list(segment.get("graph_nodes") or [])
    edges = list(segment.get("edge_sequence") or [])
    loops = []
    seen = {}
    for i, edge in enumerate(edges):
        key = _edge_key(edge)
        if key in seen:
            first = seen[key]
            # The repeated edge is the physical A->B->A signature.  The node
            # immediately before the first traversal is the natural branch
            # candidate for BB.
            if first < len(nodes) and i + 1 < len(nodes):
                branch = nodes[first]
            else:
                branch = None
            loops.append({"edge": key, "first_edge_index": first, "second_edge_index": i, "branch_node": branch})
        else:
            seen[key] = i
    return loops


def _sfc_candidate(segment, limit):
    nodes = list(segment.get("graph_nodes") or [])
    raw_points = segment.get("points") or []
    if len(nodes) < 2:
        return None
    # graph node positions are stored in the route engine coordinate system;
    # use the cumulative Pole Edge distance already represented by the route.
    edge_lengths = []
    try:
        engine = segment.get("_engine")
        if engine is not None:
            for edge in segment.get("edge_sequence") or []:
                edge_lengths.append(float(engine.edge_len.get(edge, 0.0)))
    except Exception:
        edge_lengths = []
    if len(edge_lengths) != len(nodes) - 1:
        return None
    cumulative = 0.0
    candidate = None
    for i, length in enumerate(edge_lengths):
        next_total = cumulative + length
        if next_total <= limit + 1e-6:
            candidate = {"node_index": i + 1, "distance": next_total}
        else:
            break
        cumulative = next_total
    if candidate is None:
        return {"node_index": 0, "distance": 0.0, "blocked_first_edge": True}
    if candidate["node_index"] < len(nodes):
        candidate["point"] = nodes[candidate["node_index"]]
    return candidate


def analyze_designs(designs, engine, payload, write_nodes=False):
    """Analyse ODN 2.1 constraints and return a compact planning summary.

    The returned candidates are intentionally based on the existing Pole Edge
    graph.  `write_nodes=True` writes only point-node candidates into already
    configured BB/SFC layers; it never creates or changes project configuration.
    """
    if not is_odn21(payload):
        return {"enabled": False, "bb": [], "sfc": [], "violations": []}

    lim = limits(payload)
    result = {"enabled": True, "limits": lim, "bb": [], "sfc": [], "violations": []}
    _log(
        f"[ODN2.1 START] designs={len(designs or [])}; "
        f"BB enabled={int(enabled(payload, 'BB'))}; SFC enabled={int(enabled(payload, 'SFC Closure'))}; "
        f"return_limit={lim['return_cable']:.3f}m; BB_trigger={lim['bb_trigger']:.3f}m; "
        f"prelinked_limit={lim['prelinked']:.3f}m"
    )

    for di, design in enumerate(designs or []):
        for si, original in enumerate(design.get("segments", []) or []):
            segment = dict(original)
            segment["_engine"] = engine
            distance = float(segment.get("distance", 0.0) or 0.0)
            loops = _find_return_loops(segment)
            for loop in loops:
                # A repeated edge means the same physical Pole Edge is used in
                # both directions.  Its own edge length is the complete return
                # pair length.
                edge_len = float(engine.edge_len.get(loop["edge"], 0.0) or 0.0)
                return_len = edge_len * 2.0
                item = {
                    "design": di,
                    "segment": si,
                    "edge": loop["edge"],
                    "edge_length": edge_len,
                    "return_length": return_len,
                    "branch_node": loop.get("branch_node"),
                    "needs_bb": return_len > lim["bb_trigger"],
                }
                result["bb"].append(item)
                _log(
                    f"[BB CHECK] design={di}; segment={si + 1}; edge={loop['edge']}; "
                    f"one_way={edge_len:.3f}m; return={return_len:.3f}m; "
                    f"trigger={lim['bb_trigger']:.3f}m; needs_bb={int(item['needs_bb'])}; "
                    f"branch={loop.get('branch_node')}"
                )
                if item["needs_bb"]:
                    result["violations"].append(("BB", di, si, item))

            # SFC is evaluated per actual ODN-node segment.  If this segment
            # itself is already <= the configured maximum, there is no SFC
            # action.  For an overlength segment, select the last graph node
            # whose accumulated Pole Edge distance is <= the limit.
            if distance > lim["prelinked"] + 1e-6:
                candidate = _sfc_candidate(segment, lim["prelinked"])
                item = {"design": di, "segment": si, "segment_distance": distance, "candidate": candidate}
                result["sfc"].append(item)
                if candidate and not candidate.get("blocked_first_edge"):
                    _log(
                        f"[SFC CHECK] design={di}; segment={si + 1}; length={distance:.3f}m; "
                        f"limit={lim['prelinked']:.3f}m; candidate_node_index={candidate['node_index']}; "
                        f"candidate_distance={candidate['distance']:.3f}m; point={candidate.get('point')}"
                    )
                else:
                    _log(
                        f"[SFC CHECK] design={di}; segment={si + 1}; length={distance:.3f}m; "
                        f"limit={lim['prelinked']:.3f}m; NO POLE NODE <= limit",
                        Qgis.Warning,
                    )
                result["violations"].append(("SFC Closure", di, si, item))

    _log(
        f"[ODN2.1 END] BB candidates={len(result['bb'])}; SFC candidates={len(result['sfc'])}; "
        f"violations={len(result['violations'])}"
    )
    return result


def run_diagnostics(designs, engine, payload):
    """Public entry used by Link Design; deliberately no UI/config changes."""
    try:
        return analyze_designs(designs, engine, payload, write_nodes=False)
    except Exception as exc:
        _log(f"[ODN2.1 ERROR] {type(exc).__name__}: {exc}", Qgis.Critical)
        return {"enabled": is_odn21(payload), "error": str(exc), "bb": [], "sfc": [], "violations": []}
