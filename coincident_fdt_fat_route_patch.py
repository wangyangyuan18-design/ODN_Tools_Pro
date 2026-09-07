# -*- coding: utf-8 -*-
"""Treat coincident FDT/FAT as a valid zero-length Link segment.

The Link remains valid when FDT and the first FAT share the exact same point.
The logical Segment keeps distance=0 and no Pole Edge edge sequence.  Offset
processing can then move the FAT onto the offset route; the existing FAT
landing stage expands that zero-length Segment into a real output geometry.
"""

from qgis.core import QgsMessageLog, Qgis, QgsPointXY

from . import link_design_v17 as _v17

_INSTALLED = False
_ORIGINAL_RESTORE = _v17._restore_authoritative_routes


def _log(message, level=Qgis.Info):
    try:
        QgsMessageLog.logMessage(str(message), "ODN_Tools_Pro / Link Design", level)
    except Exception:
        pass


def _coincident_point(engine, first, second, tolerance_m=1e-6):
    """Return the common point in Pole Edge CRS when two endpoints coincide."""
    try:
        if {str(first[0]).upper(), str(second[0]).upper()} != {"FDT", "FAT"}:
            return None
        _, first_info = engine.point_by_id(str(first[0]), int(first[1]))
        _, second_info = engine.point_by_id(str(second[0]), int(second[1]))
        if first_info is None or second_info is None:
            return None
        first_point = engine._point_in_edge_crs(first_info)
        second_point = engine._point_in_edge_crs(second_info)
        if engine._point_distance(first_point, second_point) > tolerance_m:
            return None
        return QgsPointXY(first_point)
    except Exception:
        return None


def _restore_authoritative_routes(host, persist=True):
    """Restore completed routes, accepting coincident FDT/FAT as valid 0m segments."""
    controller = host._controller
    engine = controller._engine or controller._prepare_engine()
    if engine is None:
        raise RuntimeError("无法建立当前 Pole Edge 路由引擎，不能恢复已完成设计。")
    controller._engine = engine

    try:
        payload = controller._v9_payload()
    except Exception:
        from . import odn_project_context as context
        payload = context.current_payload() or {}

    from . import odn_project_context as context
    edge_layer = context.project_layer(payload, "Pole Edge")
    if edge_layer is None:
        raise RuntimeError("当前项目没有绑定 Pole Edge 图层。")
    edge_authid = edge_layer.crs().authid()

    restored_links = 0
    restored_segments = 0
    failed_links = 0
    changed = False
    diagnostic_details = None
    diagnostic_failure_count = 0

    for design_index, design in enumerate(controller._designs or []):
        seq_ids = design.get("sequence_ids", []) or []
        segments = design.get("segments", []) or []
        if len(seq_ids) < 2 or len(segments) != len(seq_ids) - 1:
            continue

        rebuilt_segments = []
        link_failed = False
        for segment_index, (old_segment, first, second) in enumerate(
            zip(segments, seq_ids[:-1], seq_ids[1:])
        ):
            common_point = _coincident_point(engine, first, second)
            if common_point is not None:
                rebuilt = dict(old_segment)
                rebuilt["from"] = str(first[2]) if len(first) >= 3 else old_segment.get("from", first[1])
                rebuilt["to"] = str(second[2]) if len(second) >= 3 else old_segment.get("to", second[1])
                rebuilt["distance"] = 0.0
                rebuilt["pole_edge_distance"] = 0.0
                rebuilt["edge_sequence"] = []
                rebuilt["edge_count"] = 0
                rebuilt["points"] = [[float(common_point.x()), float(common_point.y())]]
                rebuilt["zero_length"] = True
                rebuilt_segments.append(rebuilt)
                continue

            try:
                route = engine.route(str(first[0]), int(first[1]), str(second[0]), int(second[1]))
            except Exception as exc:
                failed_links += 1
                diagnostic_failure_count += 1
                if diagnostic_details is None:
                    diagnostic_details = {
                        "design": design_index,
                        "segment": segment_index,
                        "first": first,
                        "second": second,
                        "kind": "route-exception",
                        "exception": f"{type(exc).__name__}:{exc}",
                        "start": _v17._route_attach_diagnostic(engine, str(first[0]), int(first[1])),
                        "end": _v17._route_attach_diagnostic(engine, str(second[0]), int(second[1])),
                    }
                link_failed = True
                break

            if not route or not route.get("edge_sequence"):
                failed_links += 1
                diagnostic_failure_count += 1
                if diagnostic_details is None:
                    start_diag = _v17._route_attach_diagnostic(engine, str(first[0]), int(first[1]))
                    end_diag = _v17._route_attach_diagnostic(engine, str(second[0]), int(second[1]))
                    kind = (
                        "attachment"
                        if start_diag["status"] != "attached" or end_diag["status"] != "attached"
                        else "graph-disconnected-or-no-path"
                    )
                    diagnostic_details = {
                        "design": design_index,
                        "segment": segment_index,
                        "first": first,
                        "second": second,
                        "kind": kind,
                        "exception": None,
                        "start": start_diag,
                        "end": end_diag,
                    }
                link_failed = True
                break

            rebuilt = dict(old_segment)
            rebuilt["from"] = route.get("from_label", old_segment.get("from", first[1]))
            rebuilt["to"] = route.get("to_label", old_segment.get("to", second[1]))
            rebuilt["distance"] = round(float(route["distance"]), 3)
            rebuilt["pole_edge_distance"] = round(float(route.get("pole_edge_distance", route["distance"])), 3)
            rebuilt["edge_sequence"] = list(route["edge_sequence"])
            rebuilt["edge_count"] = len(route["edge_sequence"])
            rebuilt["points"] = [[float(p.x()), float(p.y())] for p in route["points"]]
            rebuilt["zero_length"] = False
            rebuilt_segments.append(rebuilt)

        if link_failed:
            continue

        new_length = round(
            sum(float(s.get("distance", 0.0) or 0.0) for s in rebuilt_segments),
            3,
        )
        old_length = float(design.get("length", 0.0) or 0.0)
        old_segments = design.get("segments", []) or []
        if rebuilt_segments != old_segments or abs(new_length - old_length) > 0.001:
            changed = True
        design["segments"] = rebuilt_segments
        design["length"] = new_length
        design["source_crs"] = edge_authid
        restored_links += 1
        restored_segments += len(rebuilt_segments)
        design["needs_resync"] = True
        design["written"] = False

    if diagnostic_details is not None:
        detail = diagnostic_details
        _log(
            "[route-restore-diagnostic] "
            f"design={detail['design']}; segment={detail['segment']}; "
            f"{detail['first'][0]}/{detail['first'][1]} -> {detail['second'][0]}/{detail['second'][1]}; "
            f"kind={detail['kind']}; "
            f"start=({_v17._format_route_attach_diag(detail['start'])}); "
            f"end=({_v17._format_route_attach_diag(detail['end'])})"
            + (f"; exception={detail['exception']}" if detail.get('exception') else "")
            + f"; same_failures_in_run={diagnostic_failure_count}",
            Qgis.Warning,
        )

    if changed and persist:
        controller._persist_state()
    _log(
        f"[route-restore] links={restored_links}; segments={restored_segments}; "
        f"failed_links={failed_links}; changed={int(changed)}; crs={edge_authid}"
    )
    return restored_links, restored_segments, failed_links, changed


def install_coincident_fdt_fat_route_patch():
    global _INSTALLED
    if _INSTALLED:
        return
    _v17._restore_authoritative_routes = _restore_authoritative_routes
    _INSTALLED = True
