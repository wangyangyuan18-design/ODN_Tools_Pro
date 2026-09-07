# -*- coding: utf-8 -*-
"""Link Design v16: explicit multi-angle offset write and runtime compatibility fixes."""

import copy

from qgis.PyQt import QtWidgets
from qgis.core import QgsMessageLog, Qgis

from . import link_design_v15 as _v15
from . import link_design_v9 as _v9
from . import link_design_v12 as _v12
from . import odn_project_context as context
from . import cable_offset_layout_v4 as _offset
from .change_detection_adapter import save_snapshot

if not hasattr(_v9, "_fresh_payload"):
    _v9._fresh_payload = _v9._v2._fresh_payload
if not hasattr(_v9, "context"):
    _v9.context = context

_ORIGINAL_START_DESIGN = _v9._CoreController.start_design


def _v16_log(message):
    try:
        QgsMessageLog.logMessage(str(message), "ODN_Tools_Pro / Cable Offset", Qgis.Info)
    except Exception:
        pass


def _v16_start_design(self):
    host = getattr(self, "host", None)
    if host is not None:
        try:
            if not host._check_dc_consistency_before_design():
                self.status.setText("状态：Distribution Cable 与已完成设计不一致，已阻止继续链路设计。")
                return False
        except Exception as exc:
            self.status.setText(f"状态：链路数据自检失败，已阻止继续设计：{exc}")
            QtWidgets.QMessageBox.warning(host, "Link 数据检查", f"链路数据自检失败，暂不能进入链路设计：\n{exc}")
            return False
    return _ORIGINAL_START_DESIGN(self)


_v9._CoreController.start_design = _v16_start_design


class LinkDesignDock(_v15.LinkDesignDock):
    def _check_dc_consistency_before_design(self, layer=None):
        try:
            if layer is None:
                layer = context.project_layer(self._controller._v9_payload(), "Distribution Cable")
        except Exception as exc:
            raise RuntimeError(f"无法读取 Distribution Cable 图层：{exc}") from exc
        if layer is None:
            return True
        return _v12._show_startup_sync_dialog(self._controller, layer)

    def showEvent(self, event):
        super().showEvent(event)

    def _ensure_offset_edge_sequences(self, controller, edge_layer):
        """Backfill legacy edge_sequence and normalize route-point CRS.

        The routing engine creates both edge_sequence endpoints and route points
        in the current Pole Edge CRS. Older saved Links may carry a stale
        source_crs value; that metadata must not survive into offsetting.
        """
        migrated_designs = 0
        migrated_segments = 0
        failed = []
        engine = controller._engine or controller._prepare_engine()
        if engine is None:
            raise RuntimeError("无法建立当前 Pole Edge 路由引擎，不能恢复 Link 的 Edge 序列。")
        controller._engine = engine
        edge_authid = edge_layer.crs().authid()

        for design_index, design in enumerate(controller._designs or []):
            # Normalize all route coordinates to the actual Pole Edge CRS.
            # This is essential for historical designs whose source_crs was
            # recorded incorrectly before the current route-storage rule.
            if design.get("source_crs") != edge_authid:
                design["source_crs"] = edge_authid
                migrated_designs += 1

            seq_ids = design.get("sequence_ids", []) or []
            segments = design.get("segments", []) or []
            if len(seq_ids) < 2 or len(segments) != len(seq_ids) - 1:
                continue

            changed = False
            new_segments = []
            for segment_index, (segment, first, second) in enumerate(zip(segments, seq_ids[:-1], seq_ids[1:])):
                edge_sequence = segment.get("edge_sequence", []) or []
                edge_count = int(segment.get("edge_count", 0) or 0)
                if edge_sequence and (edge_count <= 0 or len(edge_sequence) == edge_count):
                    # Existing edge refs are sufficient. Keep geometry as-is;
                    # the offset engine now interprets these points in edge CRS.
                    new_segments.append(segment)
                    continue

                try:
                    route = engine.route(str(first[0]), int(first[1]), str(second[0]), int(second[1]))
                except Exception as exc:
                    failed.append((design_index, segment_index, str(first[1]), str(second[1]), f"route异常:{exc}"))
                    new_segments.append(segment)
                    continue

                if not route or not route.get("edge_sequence"):
                    failed.append((design_index, segment_index, str(first[1]), str(second[1]), "无法恢复edge_sequence"))
                    new_segments.append(segment)
                    continue

                rebuilt = dict(segment)
                rebuilt["edge_sequence"] = list(route["edge_sequence"])
                rebuilt["edge_count"] = len(route["edge_sequence"])
                rebuilt["points"] = [[float(p.x()), float(p.y())] for p in route["points"]]
                rebuilt["distance"] = round(float(route["distance"]), 3)
                rebuilt["pole_edge_distance"] = round(float(route.get("pole_edge_distance", route["distance"])), 3)
                rebuilt["from"] = route.get("from_label", rebuilt.get("from", first[1]))
                rebuilt["to"] = route.get("to_label", rebuilt.get("to", second[1]))
                new_segments.append(rebuilt)
                changed = True
                migrated_segments += 1

            if changed:
                design["segments"] = new_segments
                design["length"] = round(sum(float(s.get("distance", 0.0) or 0.0) for s in new_segments), 3)

        _v16_log(
            f"[migration] normalized_links={migrated_designs}; rebuilt_segments={migrated_segments}; "
            f"failed={len(failed)}; edge_crs={edge_authid}"
        )
        for item in failed[:12]:
            _v16_log(f"[migration-fail] design={item[0]}; segment={item[1]}; {item[2]}->{item[3]}; reason={item[4]}")
        if len(failed) > 12:
            _v16_log(f"[migration-fail] ... {len(failed) - 12} more")
        return migrated_designs, migrated_segments, len(failed)

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

        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
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
            QtWidgets.QMessageBox.warning(self, "偏移并写入图层", "当前项目缺少 Distribution Cable 或 Pole Edge 图层。")
            return
        if not dc_layer.isEditable() and not dc_layer.startEditing():
            QtWidgets.QMessageBox.warning(self, "偏移并写入图层", "无法进入 Distribution Cable 编辑状态。")
            return

        backup = copy.deepcopy(controller._designs)
        try:
            _v12._prepare_design_identities(controller._designs)
            migrated_designs, migrated_segments, failed_segments = self._ensure_offset_edge_sequences(controller, edge_layer)
            _v16_log(
                f"[input] designs={len(controller._designs)}; DC={dc_layer.featureCount()}; "
                f"PoleEdge={edge_layer.featureCount()}; spacing={spacing_value:.3f}; angles={sorted(chosen)}; "
                f"normalized_links={migrated_designs}; rebuilt_segments={migrated_segments}; failed={failed_segments}"
            )

            summary = _offset.apply_explicit_layout_to_designs(
                controller._designs,
                dc_layer,
                edge_layer,
                spacing=spacing_value,
                angles=chosen,
            )

            for index, design in enumerate(controller._designs):
                if not (design.get("segments") or []):
                    continue
                _v12._write_design_to_dc(controller, dc_layer, index, design)
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
                    "已完成写入，但本次没有产生需要偏移的重叠 Pole Edge 段。\n\n按当前规则，非重叠线路保持原位置。",
                )
        except Exception as exc:
            controller._designs = backup
            try:
                controller._persist_state()
            except Exception:
                pass
            QtWidgets.QMessageBox.warning(self, "偏移并写入图层", f"偏移写入失败，已恢复本次设计数据：\n{exc}")
