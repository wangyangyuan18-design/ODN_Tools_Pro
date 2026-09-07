# -*- coding: utf-8 -*-
"""Link Design v16: explicit multi-angle offset write and runtime compatibility fixes."""

import copy

from qgis.PyQt import QtWidgets

from . import link_design_v15 as _v15
from . import link_design_v9 as _v9
from . import link_design_v12 as _v12
from . import odn_project_context as context
from . import cable_offset_layout_v4 as _offset
from .change_detection_adapter import save_snapshot


# Compatibility aliases used by legacy adapters that imported these helpers
# from link_design_v9. The real implementation lives in link_design_v2/v3.
if not hasattr(_v9, "_fresh_payload"):
    _v9._fresh_payload = _v9._v2._fresh_payload
if not hasattr(_v9, "context"):
    _v9.context = context


_ORIGINAL_START_DESIGN = _v9._CoreController.start_design


def _v16_start_design(self):
    """Block new planning until the DC/saved-Link consistency check passes."""
    host = getattr(self, "host", None)
    if host is not None:
        try:
            if not host._check_dc_consistency_before_design():
                self.status.setText("状态：Distribution Cable 与已完成设计不一致，已阻止继续链路设计。")
                return False
        except Exception as exc:
            self.status.setText(f"状态：链路数据自检失败，已阻止继续设计：{exc}")
            QtWidgets.QMessageBox.warning(
                host,
                "Link 数据检查",
                f"链路数据自检失败，暂不能进入链路设计：\n{exc}",
            )
            return False
    return _ORIGINAL_START_DESIGN(self)


_v9._CoreController.start_design = _v16_start_design


class LinkDesignDock(_v15.LinkDesignDock):
    def _check_dc_consistency_before_design(self):
        """Run the DC/saved-Link consistency gate without swallowing errors."""
        try:
            layer = context.project_layer(
                self._controller._v9_payload(),
                "Distribution Cable",
            )
        except Exception as exc:
            raise RuntimeError(f"无法读取 Distribution Cable 图层：{exc}") from exc
        if layer is None:
            return True
        return _v12._show_startup_sync_dialog(self._controller, layer)

    def showEvent(self, event):
        super().showEvent(event)
        try:
            # The v12 compatibility layer also performs this check. Run it
            # explicitly here so a failed check cannot be silently swallowed.
            if getattr(self, "_controller", None) is not None:
                ok = self._check_dc_consistency_before_design()
                if not ok:
                    self.info.setText("Distribution Cable 与已完成设计不一致，请先完成数据同步。")
        except Exception as exc:
            self.info.setText(f"链路数据自检失败：{exc}")
            QtWidgets.QMessageBox.warning(
                self,
                "Link 数据检查",
                f"链路数据自检失败，暂不能开始链路设计：\n{exc}",
            )

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

        backup = copy.deepcopy(controller._designs)
        try:
            _v12._prepare_design_identities(controller._designs)
            summary = _offset.apply_explicit_layout_to_designs(
                controller._designs,
                dc_layer,
                edge_layer,
                spacing=spacing_value,
                angles=chosen,
            )

            # Write the already-offset geometry directly. Do not call the
            # normal writer, which may perform another automatic layout pass.
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
                    "已完成写入，但本次没有产生需要偏移的重叠 Pole Edge 段。\n\n"
                    "按当前规则，非重叠线路保持原位置。",
                )
        except Exception as exc:
            controller._designs = backup
            try:
                controller._persist_state()
            except Exception:
                pass
            QtWidgets.QMessageBox.warning(
                self,
                "偏移并写入图层",
                f"偏移写入失败，已恢复本次设计数据：\n{exc}",
            )
