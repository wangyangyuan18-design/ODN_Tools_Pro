# -*- coding: utf-8 -*-
"""Lightweight ODN 2.1 constraint planning.

ODN 2.1 keeps the existing Pole Edge route engine. This module adds the
engineering checks for BB and SFC Closure without changing the Project
Configuration UI or replacing the existing Link/offset geometry engine.
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
    """Read the existing project configuration; never creates UI settings."""
    if not is_odn21(payload):
        return False
    registry = (payload or {}).get("layer_registry") or {}
    if role in registry and registry.get(role, {}).get("layer_id"):
        return True
    for key in ("node_types", "enabled_nodes", "access_node_types", "node_type"):
        values = (payload or {}).get(key)
        if isinstance(values, dict) and values.get(role) is True:
            return True
        if isinstance(values, (list, tuple, set)) and role in values:
            return True
    return False


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


def _find_return_loops(segment):
    """Find repeated Pole Edge traversals: A->B ... B->A."""
    nodes = list(segment.get("graph_nodes") or [])
    edges = list(segment.get("edge_sequence") or [])
    loops = []
    seen = {}
    for i, edge in enumerate(edges):
        if edge in seen:
            first = seen[edge]
            branch = nodes[first] if first < len(nodes) else None
            loops.append({
                "edge": edge,
                "first_edge_index": first,
                "second_edge_index": i,
                "branch_node": branch,
            })
        else:
            seen[edge] = i
    return loops


def _sfc_candidate(segment, engine, limit):
    """Return the last actual Pole node whose cumulative route <= limit."""
    nodes = list(segment.get("graph_nodes") or [])
    edges = list(segment.get("edge_sequence") or [])
    if len(nodes) < 2 or len(edges) != len(nodes) - 1:
        return None
    cumulative = 0.0
    candidate = None
    for i, edge in enumerate(edges):
        length = float(engine.edge_len.get(edge, 0.0) or 0.0)
        if cumulative + length > limit + 1e-6:
            break
        cumulative += length
        candidate = {
            "node_index": i + 1,
            "distance": cumulative,
            "node": nodes[i + 1],
        }
    if candidate is None:
        return {"blocked_first_edge": True, "distance": 0.0}
    return candidate


def analyze_designs(designs, engine, payload):
    """Analyse ODN 2.1 constraints on the authoritative Pole Edge routes."""
    if not is_odn21(payload):
        return {"enabled": False, "bb": [], "sfc": [], "violations": []}

    lim = limits(payload)
    result = {
        "enabled": True,
        "limits": lim,
        "bb": [],
        "sfc": [],
        "violations": [],
    }
    _log(
        f"[ODN2.1 START] designs={len(designs or [])}; "
        f"BB={int(enabled(payload, 'BB'))}; SFC Closure={int(enabled(payload, 'SFC Closure'))}; "
        f"return_limit={lim['return_cable']:.3f}m; BB_trigger={lim['bb_trigger']:.3f}m; "
        f"DC_limit={lim['prelinked']:.3f}m"
    )

    for di, design in enumerate(designs or []):
        for si, segment in enumerate(design.get("segments", []) or []):
            distance = float(segment.get("distance", 0.0) or 0.0)
            loops = _find_return_loops(segment)
            for loop in loops:
                edge_len = float(engine.edge_len.get(loop["edge"], 0.0) or 0.0)
                return_len = edge_len * 2.0
                item = {
                    "design": di,
                    "segment": si,
                    "edge": loop["edge"],
                    "one_way": edge_len,
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

            if distance > lim["prelinked"] + 1e-6:
                candidate = _sfc_candidate(segment, engine, lim["prelinked"])
                item = {
                    "design": di,
                    "segment": si,
                    "segment_distance": distance,
                    "candidate": candidate,
                }
                result["sfc"].append(item)
                if candidate and not candidate.get("blocked_first_edge"):
                    _log(
                        f"[SFC CHECK] design={di}; segment={si + 1}; length={distance:.3f}m; "
                        f"limit={lim['prelinked']:.3f}m; last_ok={candidate['distance']:.3f}m; "
                        f"node_index={candidate['node_index']}; node={candidate['node']}"
                    )
                else:
                    _log(
                        f"[SFC CHECK] design={di}; segment={si + 1}; length={distance:.3f}m; "
                        f"limit={lim['prelinked']:.3f}m; NO POLE NODE <= limit",
                        Qgis.Warning,
                    )
                result["violations"].append(("SFC Closure", di, si, item))

    _log(
        f"[ODN2.1 END] return_loops={len(result['bb'])}; "
        f"BB_required={sum(1 for x in result['bb'] if x['needs_bb'])}; "
        f"SFC_required={len(result['sfc'])}; violations={len(result['violations'])}"
    )
    return result


def run_diagnostics(designs, engine, payload):
    try:
        return analyze_designs(designs, engine, payload)
    except Exception as exc:
        _log(f"[ODN2.1 ERROR] {type(exc).__name__}: {exc}", Qgis.Critical)
        return {"enabled": is_odn21(payload), "error": str(exc), "bb": [], "sfc": [], "violations": []}


def install_link_design_odn21(LinkDesignDock):
    """Install a non-invasive diagnostic hook; UI and routing remain unchanged."""
    if getattr(LinkDesignDock, "_odn21_hook_installed", False):
        return

    original_show = LinkDesignDock.showEvent

    def show_event(self, event):
        try:
            controller = getattr(self, "_controller", None)
            payload = controller._v9_payload() if controller is not None else None
            if is_odn21(payload) and controller is not None and controller._designs:
                result = run_diagnostics(controller._designs, controller._engine or controller._prepare_engine(), payload)
                self._odn21_last_plan = result
        except Exception as exc:
            _log(f"[HOOK ERROR] showEvent: {type(exc).__name__}: {exc}", Qgis.Warning)
        return original_show(self, event)

    LinkDesignDock.showEvent = show_event
    LinkDesignDock._odn21_hook_installed = True
