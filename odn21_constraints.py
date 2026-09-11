# -*- coding: utf-8 -*-
"""ODN 2.1 planning constraints.

This module is intentionally small: it does not change Project Configuration
or the existing Pole Edge/offset UI.  It supplies Link Design with the two
ODN 2.1 placement rules:

1. BB: detect return-cable structures and select a branch node candidate.
2. SFC Closure: on each DC segment, select the last real pole node whose
   accumulated route distance does not exceed the configured limit.

The geometry writer remains responsible for final line geometry and offset.
"""
from qgis.core import QgsMessageLog, Qgis

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

def is_odn21(payload):
    version = str(((payload or {}).get("project") or {}).get("odn_version", "")).strip().lower()
    return version in ("2.1", "odn 2.1", "odn2.1", "odn_2.1")

def enabled(payload, role):
    """Read existing project configuration; never changes the configuration UI."""
    if not is_odn21(payload):
        return False
    registry = payload.get("layer_registry") or {}
    for key in (role, role.lower(), role.replace(" ", "_"), role.lower().replace(" ", "_")):
        item = registry.get(key)
        if isinstance(item, dict) and item.get("layer_id"):
            return True
    for key in ("node_types", "enabled_nodes", "access_node_types", "node_type"):
        values = payload.get(key)
        if isinstance(values, dict) and values.get(role) is True:
            return True
        if isinstance(values, (list, tuple, set)) and role in values:
            return True
    return False

def limits(payload):
    return {
        "return_cable": _number(payload, ("return_cable_max_distance", "return_cable_limit", "back_cable_max_distance", "back_cable_limit"), 100.0),
        "bb_trigger": _number(payload, ("bb_trigger_distance", "bb_return_cable_trigger", "bb_limit"), 150.0),
        "dc": _number(payload, ("prelinked_cable_max_length", "pre_linked_cable_max_length", "dc_cable_max_length", "optical_cable_max_length"), 455.0),
    }

def _edge_key(edge):
    try:
        return ("id", int(edge.id()))
    except Exception:
        return str(edge)

def _return_loops(segment):
    """Return repeated physical-edge traversals (A->B->A)."""
    edges = list(segment.get("edge_sequence") or [])
    nodes = list(segment.get("graph_nodes") or [])
    first_seen = {}
    loops = []
    for i, edge in enumerate(edges):
        key = _edge_key(edge)
        if key in first_seen:
            j = first_seen[key]
            branch = nodes[j] if j < len(nodes) else None
            loops.append({"edge": edge, "edge_key": key, "first": j, "second": i, "branch_node": branch})
        else:
            first_seen[key] = i
    return loops

def _edge_length(engine, edge):
    key = _edge_key(edge)
    try:
        if key in engine.edge_len:
            return float(engine.edge_len[key])
    except Exception:
        pass
    try:
        return float(engine.edge_len.get(edge, 0.0))
    except Exception:
        return 0.0

def _last_pole_at_limit(segment, engine, limit):
    nodes = list(segment.get("graph_nodes") or [])
    edges = list(segment.get("edge_sequence") or [])
    if len(nodes) < 2 or len(edges) != len(nodes) - 1:
        return None
    cumulative = 0.0
    candidate = None
    for i, edge in enumerate(edges):
        length = _edge_length(engine, edge)
        nxt = cumulative + length
        if nxt <= limit + 1e-6:
            candidate = {"node_index": i + 1, "distance": nxt, "point": nodes[i + 1]}
            cumulative = nxt
        else:
            break
    return candidate

def analyze_designs(designs, engine, payload):
    """Return ODN 2.1 diagnostics/candidates without changing geometry."""
    if not is_odn21(payload):
        return {"enabled": False, "bb": [], "sfc": [], "violations": []}
    lim = limits(payload)
    result = {"enabled": True, "limits": lim, "bb": [], "sfc": [], "violations": []}
    bb_on = enabled(payload, "BB")
    sfc_on = enabled(payload, "SFC Closure")
    _log(f"[ODN2.1 START] designs={len(designs or [])}; BB={int(bb_on)}; SFC Closure={int(sfc_on)}; return_limit={lim['return_cable']:.3f}m; BB_trigger={lim['bb_trigger']:.3f}m; DC_limit={lim['dc']:.3f}m")

    for di, design in enumerate(designs or []):
        for si, segment in enumerate(design.get("segments", []) or []):
            if bb_on:
                for loop in _return_loops(segment):
                    one_way = _edge_length(engine, loop["edge"])
                    ret = one_way * 2.0
                    item = {"design": di, "segment": si, "edge_key": loop["edge_key"], "one_way": one_way, "return_length": ret, "branch_node": loop["branch_node"], "needs_bb": ret > lim["bb_trigger"]}
                    result["bb"].append(item)
                    _log(f"[BB CHECK] design={di}; segment={si + 1}; one_way={one_way:.3f}m; return={ret:.3f}m; trigger={lim['bb_trigger']:.3f}m; needs_bb={int(item['needs_bb'])}; branch_node={loop['branch_node']}")
                    if item["needs_bb"]:
                        result["violations"].append(("BB", di, si, item))

            if sfc_on:
                distance = float(segment.get("distance", 0.0) or 0.0)
                if distance > lim["dc"] + 1e-6:
                    candidate = _last_pole_at_limit(segment, engine, lim["dc"])
                    item = {"design": di, "segment": si, "segment_distance": distance, "candidate": candidate}
                    result["sfc"].append(item)
                    if candidate:
                        _log(f"[SFC CHECK] design={di}; segment={si + 1}; length={distance:.3f}m; limit={lim['dc']:.3f}m; selected_node={candidate['node_index']}; selected_distance={candidate['distance']:.3f}m; point={candidate['point']}")
                    else:
                        _log(f"[SFC CHECK] design={di}; segment={si + 1}; length={distance:.3f}m; limit={lim['dc']:.3f}m; NO NODE <= LIMIT", Qgis.Warning)
                    result["violations"].append(("SFC Closure", di, si, item))

    _log(f"[ODN2.1 END] BB_checks={len(result['bb'])}; SFC_checks={len(result['sfc'])}; violations={len(result['violations'])}")
    return result

def run_diagnostics(designs, engine, payload):
    try:
        return analyze_designs(designs, engine, payload)
    except Exception as exc:
        _log(f"[ODN2.1 ERROR] {type(exc).__name__}: {exc}", Qgis.Critical)
        return {"enabled": is_odn21(payload), "error": str(exc), "bb": [], "sfc": [], "violations": []}
