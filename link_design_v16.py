# -*- coding: utf-8 -*-
"""Link Design v16: explicit multi-angle offset write fix."""
import copy
from qgis.PyQt import QtWidgets
from qgis.core import QgsProject
from . import link_design_v15 as _v15
from . import link_design_v12 as _v12
from . import cable_offset_layout_v4 as _offset
from .change_detection_adapter import save_snapshot


class LinkDesignDock(_v15.LinkDesignDock):
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
        dc_layer = _v12.context.project_layer(payload, "Distribution Cable")
        edge_layer = _v12.context.project_layer(payload, "Pole Edge")
        if dc_layer is None or edge_layer is None:
            QtWidgets.QMessageBox.warning(self, "偏移并写入图层", "当前项目缺少 Distribution Cable 或 Pole Edge 图层。")
            return
        if not dc_layer.isEditable() and not dc_layer.startEditing():
            QtWidgets.QMessageBox.warning(self, "偏移并写入图层", "无法进入 Distribution Cable 编辑状态。")
            return

        backup = copy.deepcopy(controller._designs)
        try:
            _v12._prepare_design_identities(controller._designs)
            _offset.apply_explicit_layout_to_designs(
                controller._designs,
                dc_layer,
                edge_layer,
                spacing=spacing_value,
                angles=chosen,
            )
            # Bypass the normal writer's automatic layout pass. The geometry
            # just generated above is the final requested offset geometry.
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
                f"偏移量 {spacing_value:.2f} m。"
            )
        except Exception as exc:
            controller._designs = backup
            try:
                controller._persist_state()
            except Exception:
                pass
            QtWidgets.QMessageBox.warning(self, "偏移并写入图层", f"偏移写入失败，已恢复本次设计数据：\n{exc}")
