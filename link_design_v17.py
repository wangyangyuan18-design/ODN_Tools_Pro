# -*- coding: utf-8 -*-
"""Link Design v17: keep planned routes authoritative and isolate output offset.

Design rules:
- Completed Link Design routes are rebuilt from the same Pole Edge route engine
  used by the known-good Link Design #39 baseline.
- Offset calculations operate on a deep copy only.
- The saved controller designs always retain the un-offset Pole Edge route.
- Distribution Cable receives the optional offset geometry.
- Linked Distribution Cable geometry is never imported back into the planned
  Link geometry during the startup consistency check.
"""

import copy

from qgis.PyQt import QtWidgets
from qgis.core import QgsMessageLog, Qgis

from . import link_design_v16 as _v16
from . import link_design_v9 as _v9
from . import link_design_v12 as _v12
from . import odn_project_context as context
from . import cable_offset_layout_v4 as _offset
from .change_detection_adapter import save_snapshot


_ORIGINAL_SAVE_CURRENT_LINK = _v9._CoreController.save_current_link
_ORIGINAL_START_DESIGN = _v9._CoreController.start_design


def _log(message, level=Qgis.Info):
    try:
        QgsMessageLog.logMessage(str(message), "ODN_Tools_Pro / Link Design", level)
    except Exception:
        pass


def _restore_authoritative_routes(host, persist=True):
    """Rebuild completed Link geometry exactly from FDT/FAT sequence via Pole Edge.

    This intentionally follows the #39 route construction model: every
    consecutive pair in sequence_ids is sent through the current route engine.
    Offset geometry is never used as an input here.
    """
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
                route = engine.route(
                    str(first[0]), int(first[1]),
                    str(second[0]), int(second[1]),
                )
            except Exception as exc:
                _log(
                    f"[route-restore-fail] design={design_index}; segment={segment_index}; "
                    f"{first[1]}->{second[1]}; reason={type(exc).__name__}:{exc}",
                    Qgis.Warning,
                )
                link_failed = True
                break

            if not route or not route.get("edge_sequence"):
                _log(
                    f"[route-restore-fail] design={design_index}; segment={segment_index}; "
                    f"{first[1]}->{second[1]}; reason=无法沿 Pole Edge 建立完整路径",
                    Qgis.Warning,
                )
                link_failed = True
                break

            rebuilt = dict(old_segment)
            rebuilt["from"] = route.get("from_label", old_segment.get("from", first[1]))
            rebuilt["to"] = route.get("to_label", old_segment.get("to", second[1]))
            rebuilt["distance"] = round(float(route["distance"]), 3)
            rebuilt["pole_edge_distance"] = round(
                float(route.get("pole_edge_distance", route["distance"])), 3
            )
            rebuilt["edge_sequence"] = list(route["edge_sequence"])
            rebuilt["edge_count"] = len(route["edge_sequence"])
            rebuilt["points"] = [
                [float(p.x()), float(p.y())] for p in route["points"]
            ]
            rebuilt_segments.append(rebuilt)

        if link_failed:
            failed_links += 1
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

        # The restored route is authoritative. Existing offset/output status
        # must not contaminate its geometry.
        design["needs_resync"] = True
        design["written"] = False

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
                host,
                "链路设计",
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
    """Active dock with #39-compatible authoritative routing and isolated output offset."""

    def _check_dc_consistency_before_design(self, layer=None):
        # Do not import Distribution Cable geometry into Link Design. This is
        # essential because offset output is intentionally different geometry.
        # We keep the planned Link route authoritative instead.
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

        enabled, spacing_value = _offset.get_settings()
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("偏移并写入图层")
        dialog.setModal(True)
        form = QtWidgets.QFormLayout(dialog)
        form.setContentsMargins(12, 12, 12, 12)
        form.setSpacing(8)

        angles_box = QtWidgets.QWidget(dialog)
        angles_layout = QtWidgets.QHBoxLayout(angles_box)
        angles_layout.setContentsMargins(0, 0, 0, 0)
        angles_layout.setSpacing(12)
        angle_checks = {}
        for value in (45.0, 60.0, 75.0, 90.0):
            check = QtWidgets.QCheckBox(f"{int(value)}°", angles_box)
            check.setChecked(value in enabled)
            angle_checks[value] = check
            angles_layout.addWidget(check)
        form.addRow("角度", angles_box)

        spacing = QtWidgets.QDoubleSpinBox(dialog)
        spacing.setRange(0.01, 20.0)
        spacing.setDecimals(2)
        spacing.setSingleStep(0.10)
        spacing.setValue(spacing_value)
        spacing.setSuffix(" m")
        form.addRow("偏移量", spacing)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        form.addRow(buttons)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return

        chosen = [value for value, check in angle_checks.items() if check.isChecked()]
        if not chosen:
            QtWidgets.QMessageBox.warning(self, "偏移并写入图层", "至少选择一个角度。")
            return
        spacing_value = float(spacing.value())
        _offset.save_settings(chosen, spacing_value)

        payload = controller._v9_payload()
        dc_layer = context.project_layer(payload, "Distribution Cable")
        edge_layer = context.project_layer(payload, "Pole Edge")
        if dc_layer is None or edge_layer is None:
            QtWidgets.QMessageBox.warning(
                self,
                "偏移并写入图层",
                "当前项目缺少 Distribution Cable 或 Pole Edge 图层。",
            )
            return
        if not dc_layer.isEditable() and not dc_layer.startEditing():
            QtWidgets.QMessageBox.warning(
                self,
                "偏移并写入图层",
                "无法进入 Distribution Cable 编辑状态。",
            )
            return

        # Before generating output, restore the authoritative planning route.
        # This fixes existing designs affected by the old offset contamination.
        try:
            _restore_authoritative_routes(self, persist=True)
        except Exception as exc:
            QtWidgets.QMessageBox.warning(
                self,
                "偏移并写入图层",
                f"已完成设计无法恢复为 Pole Edge 原始规划路线，因此停止写入：\n{exc}",
            )
            return

        planned_designs = copy.deepcopy(controller._designs)
        try:
            _v12._prepare_design_identities(planned_designs)
            summary = _offset.apply_explicit_layout_to_designs(
                planned_designs,
                dc_layer,
                edge_layer,
                spacing=spacing_value,
                angles=chosen,
            )

            # IMPORTANT: only the copied/offset designs are written.
            for index, design in enumerate(planned_designs):
                if not (design.get("segments") or []):
                    continue
                _v12._write_design_to_dc(controller, dc_layer, index, design)

            # Original controller designs remain un-offset and authoritative.
            for design in controller._designs:
                design["written"] = True
                design["needs_resync"] = False
                design.pop("_resync_source", None)

            dc_layer.triggerRepaint()
            controller._persist_state()
            save_snapshot(controller)
            self.refresh_from_core()
            self.info.setText(
                f"已偏移并写入全部 Link：{len(controller._designs)} 条；"
                f"角度 {', '.join(str(int(x)) + '°' for x in sorted(chosen))}，"
                f"偏移量 {spacing_value:.2f} m；"
                f"实际发生偏移 {summary.get('changed_designs', 0)} 条 Link。"
            )
            if not summary.get("changed_designs"):
                QtWidgets.QMessageBox.information(
                    self,
                    "偏移并写入图层",
                    "已完成写入，但本次没有产生需要偏移的重叠 Pole Edge 段。\n\n"
                    "非重叠线路保持原规划位置。",
                )
        except Exception as exc:
            QtWidgets.QMessageBox.warning(
                self,
                "偏移并写入图层",
                f"偏移写入失败，本次没有把偏移结果写回已完成设计：\n{exc}",
            )
