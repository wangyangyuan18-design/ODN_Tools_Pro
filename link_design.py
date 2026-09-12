# -*- coding: utf-8 -*-
"""Link Design v17: authoritative routes plus coordinated cable/FAT offset output.

Design rules:
- Completed Link Design routes are rebuilt from the same Pole Edge route engine
  used by the known-good Link Design baseline.
- Offset calculations operate on a deep copy only.
- The saved controller designs retain the un-offset Pole Edge route.
- Distribution Cable receives the optional offset geometry.
- FAT output points are recalculated from the owning Link after offset: a
  straight-through FAT lands on the Link's parallel offset; a large-corner
  FAT lands on the generated corner/control-distance bend. Cable endpoints are
  made coincident with the moved FAT.
"""

import copy

from qgis.PyQt import QtWidgets
from qgis.core import QgsMessageLog, Qgis

from . import link_design_v16 as _v16
from . import link_design_v9 as _v9
from . import link_design_v12 as _v12
from . import odn_project_context as context
from . import cable_offset_layout_v6 as _offset
from .change_detection_adapter import save_snapshot


_ORIGINAL_SAVE_CURRENT_LINK = _v9._CoreController.save_current_link
_ORIGINAL_START_DESIGN = _v9._CoreController.start_design


def _log(message, level=Qgis.Info):
    try:
        QgsMessageLog.logMessage(str(message), "ODN_Tools_Pro / Link Design", level)
    except Exception:
        pass


def _route_attach_diagnostic(engine, typ, feature_id):
    """Return one detailed attachment diagnosis without changing routing behavior."""
    result = {
        "typ": str(typ),
        "feature_id": int(feature_id),
        "status": "unknown",
        "label": str(feature_id),
        "distance": None,
        "limit": float(getattr(engine, "attach_distance", 0.0) or 0.0),
        "node": None,
    }
    try:
        _, info = engine.point_by_id(typ, feature_id)
        if info is None:
            result["status"] = "point-not-found"
            return result
        result["label"] = str(info.get("label", feature_id))
    except Exception as exc:
        result["status"] = f"point-lookup-error:{type(exc).__name__}"
        return result

    try:
        if not getattr(engine, "node_points", None):
            result["status"] = "pole-edge-graph-empty"
            return result
        point = engine._point_in_edge_crs(info)
        candidates = []
        if hasattr(engine, "_node_index"):
            candidates = engine._node_index.nearestNeighbor(point, 16)
        best = None
        for fid in candidates:
            node = engine._node_by_fid.get(int(fid))
            if node is None:
                continue
            node_point = engine.node_points[node]
            distance = engine._point_distance(point, node_point)
            if best is None or distance < best[0]:
                best = (distance, node)
        if best is None:
            result["status"] = "nearest-node-not-found"
            return result
        result["distance"] = float(best[0])
        result["node"] = best[1]
        if best[0] > result["limit"]:
            result["status"] = "over-attach-limit"
        else:
            result["status"] = "attached"
    except Exception as exc:
        result["status"] = f"diagnostic-error:{type(exc).__name__}:{exc}"
    return result


def _format_route_attach_diag(diag):
    distance = "N/A" if diag.get("distance") is None else f"{diag['distance']:.3f}m"
    node = diag.get("node")
    if isinstance(node, tuple):
        node_text = f"({node[0]:.8f},{node[1]:.8f})"
    else:
        node_text = "N/A"
    return (
        f"{diag.get('typ')}/{diag.get('feature_id')}[{diag.get('label')}]; "
        f"status={diag.get('status')}; nearest={distance}; "
        f"limit={diag.get('limit', 0.0):.3f}m; node={node_text}"
    )


def _restore_authoritative_routes(host, persist=True):
    """Rebuild completed Link geometry exactly from FDT/FAT sequence via Pole Edge."""
    controller = host._controller
    engine = controller._engine or controller._prepare_engine()
    if engine is None:
        raise RuntimeError("无法建立当前 Pole Edge 路由引擎，不能恢复已完成设计。")
    controller._engine = engine
    try:
        payload = controller._v9_payload()
    except Exception:
        payload = context.current_payload() or {}
    edge_layer = context.project_layer(payload, "Pole Edge")
    if edge_layer is None:
        raise RuntimeError("当前项目没有绑定 Pole Edge 图层。")
    edge_authid = edge_layer.crs().authid()

    restored_links = 0
    restored_segments = 0
    failed_links = 0
    changed = False
    diagnostic_emitted = False
    diagnostic_failure_count = 0
    diagnostic_details = None

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
                        "start": _route_attach_diagnostic(engine, str(first[0]), int(first[1])),
                        "end": _route_attach_diagnostic(engine, str(second[0]), int(second[1])),
                    }
                link_failed = True
                break
            if not route or not route.get("edge_sequence"):
                diagnostic_failure_count += 1
                if diagnostic_details is None:
                    start_diag = _route_attach_diagnostic(engine, str(first[0]), int(first[1]))
                    end_diag = _route_attach_diagnostic(engine, str(second[0]), int(second[1]))
                    if start_diag["status"] != "attached" or end_diag["status"] != "attached":
                        kind = "attachment"
                    else:
                        kind = "graph-disconnected-or-no-path"
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
                failed_links += 1
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
            rebuilt_segments.append(rebuilt)
        if link_failed:
            continue
        new_length = round(sum(float(s.get("distance", 0.0) or 0.0) for s in rebuilt_segments), 3)
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

    if diagnostic_details is not None and not diagnostic_emitted:
        diagnostic_emitted = True
        detail = diagnostic_details
        _log(
            "[route-restore-diagnostic] "
            f"design={detail['design']}; segment={detail['segment']}; "
            f"{detail['first'][0]}/{detail['first'][1]} -> {detail['second'][0]}/{detail['second'][1]}; "
            f"kind={detail['kind']}; "
            f"start=({_format_route_attach_diag(detail['start'])}); "
            f"end=({_format_route_attach_diag(detail['end'])})"
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


def _v17_start_design(self):
    host = getattr(self, "host", None)
    if host is not None:
        try:
            _restore_authoritative_routes(host, persist=True)
        except Exception as exc:
            self.status.setText(f"状态：无法恢复 Pole Edge 规划路线：{exc}")
            QtWidgets.QMessageBox.warning(
                host, "链路设计",
                f"当前已完成 Link 路线无法按 Pole Edge 恢复，已阻止继续规划：\n{exc}",
            )
            return False
    return _ORIGINAL_START_DESIGN(self)


_v9._CoreController.start_design = _v17_start_design


def _v17_save_current_link(self):
    result = _ORIGINAL_SAVE_CURRENT_LINK(self)
    if not result:
        return result
    host = getattr(self, "host", None)
    if host is not None:
        try:
            _restore_authoritative_routes(host, persist=True)
        except Exception as exc:
            _log(f"[route-restore-save-fail] {type(exc).__name__}:{exc}", Qgis.Critical)
            return False
    return True


_v9._CoreController.save_current_link = _v17_save_current_link


class LinkDesignDock(_v16.LinkDesignDock):
    """Active dock with authoritative routing and isolated output offset."""

    def _check_dc_consistency_before_design(self, layer=None):
        return True

    def showEvent(self, event):
        try:
            _restore_authoritative_routes(self, persist=True)
        except Exception as exc:
            _log(f"[route-restore-show-fail] {type(exc).__name__}:{exc}", Qgis.Critical)
        super().showEvent(event)

    def _offset_and_write(self):
        controller = self._controller
        if controller._draw_active:
            self.info.setText("请先保存或退出当前规划，再执行偏移并写入图层。")
            return
        if not controller._designs:
            self.info.setText("当前没有已完成的 Link 可偏移写入。")
            return

        spacing_value, control_distance_value = _offset.get_settings()
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("偏移并写入图层")
        dialog.setModal(True)
        form = QtWidgets.QFormLayout(dialog)
        form.setContentsMargins(12, 12, 12, 12)
        form.setSpacing(8)

        spacing = QtWidgets.QDoubleSpinBox(dialog)
        spacing.setRange(0.01, 20.0)
        spacing.setDecimals(2)
        spacing.setSingleStep(0.10)
        spacing.setValue(spacing_value)
        spacing.setSuffix(" m")
        form.addRow("偏移间距", spacing)

        control_distance = QtWidgets.QDoubleSpinBox(dialog)
        control_distance.setRange(0.01, 5.0)
        control_distance.setDecimals(2)
        control_distance.setSingleStep(0.05)
        control_distance.setValue(control_distance_value)
        control_distance.setSuffix(" m")
        control_distance.setToolTip(
            "拐角控制距离：Pole Edge 顶点到控制线 A 的距离。\n"
            "偏移拐点只能落在该控制线附近；实际角度由偏移量与该距离自然计算。"
        )
        form.addRow("拐角控制距离", control_distance)

        hint = QtWidgets.QLabel(
            "角度不再作为参数。\n"
            "控制距离 0.30 m 时：偏移 0.50 m ≈ 59.0°，"
            "偏移 1.00 m ≈ 73.3°，偏移 1.50 m ≈ 78.7°。\n"
            "FAT 落点：直行跟随 Link 偏移线；大拐角落在实际偏移拐点。"
        )
        hint.setWordWrap(True)
        form.addRow("规则", hint)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        form.addRow(buttons)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return

        spacing_value = float(spacing.value())
        control_distance_value = float(control_distance.value())
        _offset.save_settings(spacing_value, control_distance_value)

        payload = controller._v9_payload()
        dc_layer = context.project_layer(payload, "Distribution Cable")
        edge_layer = context.project_layer(payload, "Pole Edge")
        fat_layer = context.project_layer(payload, "FAT")
        if dc_layer is None or edge_layer is None or fat_layer is None:
            QtWidgets.QMessageBox.warning(
                self, "偏移并写入图层",
                "当前项目缺少 Distribution Cable、Pole Edge 或 FAT 图层。",
            )
            return
        if not dc_layer.isEditable() and not dc_layer.startEditing():
            QtWidgets.QMessageBox.warning(
                self, "偏移并写入图层", "无法进入 Distribution Cable 编辑状态。"
            )
            return
        if not fat_layer.isEditable() and not fat_layer.startEditing():
            QtWidgets.QMessageBox.warning(
                self, "偏移并写入图层", "无法进入 FAT 编辑状态，因此没有执行本次偏移写入。"
            )
            return

        try:
            _restore_authoritative_routes(self, persist=True)
        except Exception as exc:
            QtWidgets.QMessageBox.warning(
                self, "偏移并写入图层",
                f"已完成设计无法恢复为 Pole Edge 原始规划路线，因此停止写入：\n{exc}",
            )
            return

        planned_designs = copy.deepcopy(controller._designs)
        try:
            _v12._prepare_design_identities(planned_designs)
            fat_limit = 3.0
            try:
                fat_limit = float((payload.get("parameters") or {}).get("fat_pole_max_distance", 3.0))
            except (TypeError, ValueError):
                fat_limit = 3.0
            summary = _offset.apply_explicit_layout_to_designs(
                planned_designs,
                dc_layer,
                edge_layer,
                spacing=spacing_value,
                control_distance_m=control_distance_value,
                fat_layer=fat_layer,
                fat_max_distance_m=fat_limit,
            )

            # FAT geometry is not committed until all copied Cable geometries
            # have successfully passed validation and been written.
            for index, design in enumerate(planned_designs):
                if not (design.get("segments") or []):
                    continue
                _v12._write_design_to_dc(controller, dc_layer, index, design)

            fat_written = _offset.commit_fat_landing_points(fat_layer, summary)

            for design in controller._designs:
                design["written"] = True
                design["needs_resync"] = False
                design.pop("_resync_source", None)

            dc_layer.triggerRepaint()
            fat_layer.triggerRepaint()
            controller._persist_state()
            save_snapshot(controller)
            self.refresh_from_core()
            self.info.setText(
                f"已偏移并写入全部 Link：{len(controller._designs)} 条；"
                f"拐角控制距离 {control_distance_value:.2f} m，"
                f"偏移间距 {spacing_value:.2f} m；"
                f"Cable 实际偏移 {summary.get('changed_designs', 0)} 条 Link；"
                f"FAT 落点更新 {fat_written} 个。"
            )
            if not summary.get("changed_designs") and not fat_written:
                QtWidgets.QMessageBox.information(
                    self,
                    "偏移并写入图层",
                    "已完成写入，但本次没有产生需要偏移的重叠 Pole Edge 段。\n\n"
                    "非重叠线路保持原规划位置，FAT 也保持原落点。",
                )
            elif summary.get("fat_skipped"):
                QtWidgets.QMessageBox.information(
                    self,
                    "偏移并写入图层",
                    f"Cable 偏移和 FAT 落点更新已完成。\n\n"
                    f"FAT 总数：{summary.get('fat_total', 0)}\n"
                    f"已更新：{fat_written}\n"
                    f"直行：{summary.get('fat_straight', 0)}\n"
                    f"大拐角：{summary.get('fat_corner', 0)}\n"
                    f"跳过：{summary.get('fat_skipped', 0)}\n\n"
                    "被跳过的 FAT 不会被强制移动，并已在日志中记录原因。",
                )
        except Exception as exc:
            _log(f"[offset-write-fail] {type(exc).__name__}: {exc}", Qgis.Critical)
            QtWidgets.QMessageBox.warning(
                self,
                "偏移并写入图层",
                f"偏移写入失败，本次没有把偏移结果写回已完成设计：\n{exc}",
            )
