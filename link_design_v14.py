# -*- coding: utf-8 -*-
"""Link Design v14: automatic multi-cable Pole Edge offset layout.

This active layer also integrates project-source change detection. Change
repair preserves the saved FAT order and only rebuilds affected Link segments.
"""

from qgis.PyQt.QtCore import QSettings
from qgis.PyQt import QtWidgets

from . import link_design_v12 as _v12
from . import link_design_v9 as _v9
from .cable_offset_layout_v2 import apply_layout_to_designs
from .change_detection import ChangeDetectionDialog, detect_changes, save_snapshot


_ORIGINAL_WRITE = _v9._CoreController.write_planned_links
_ORIGINAL_SAVE = _v9._CoreController.save_current_link
SPACING_KEY = "ODNToolsPro/CableOffsetLayout/spacing_m"
DEFAULT_SPACING_M = 0.5


def _spacing_m():
    try:
        value = float(QSettings().value(SPACING_KEY, DEFAULT_SPACING_M))
    except (TypeError, ValueError):
        value = DEFAULT_SPACING_M
    return value if value > 0 else DEFAULT_SPACING_M


def _v14_save_current_link(self):
    """Save a Link and establish its source snapshot as the new baseline."""
    result = _ORIGINAL_SAVE(self)
    if result:
        try:
            save_snapshot(self)
        except Exception as exc:
            # Snapshot failure must never invalidate an already successful Link save.
            self.status.setText(f"状态：Link 已保存，但变更检测基准保存失败：{exc}")
    return result


def _v14_write_planned_links(self):
    """Lay out pending cables first, then use v12's safe cable sync writer."""
    try:
        payload = _v9._fresh_payload(self)
        distribution_layer = _v9.context.project_layer(payload, "Distribution Cable")
        edge_layer = _v9.context.project_layer(payload, "Pole Edge")
        if distribution_layer is None:
            return _ORIGINAL_WRITE(self)
        if edge_layer is None:
            return _ORIGINAL_WRITE(self)

        pending = [
            design for design in self._designs
            if not design.get("written") or design.get("needs_resync")
        ]
        if pending:
            summary = apply_layout_to_designs(
                self._designs,
                distribution_layer,
                edge_layer,
                spacing=_spacing_m(),
            )
            if summary.get("changed_designs"):
                self.status.setText(
                    "状态：已自动分配重叠线路偏移槽位；"
                    f"处理 {summary['changed_designs']} 条 Link，"
                    f"间距 {summary['spacing_m']:.2f} m。"
                )
            self._persist_state()
    except Exception as exc:
        QtWidgets.QMessageBox.warning(
            self,
            "线路偏移",
            "自动线路偏移布局失败，已保留原始 Pole Edge 路径并继续写入。\n\n"
            f"原因：{exc}",
        )

    result = _ORIGINAL_WRITE(self)
    if result:
        try:
            # The physical cable layer is now synchronized to the same design
            # state that is considered the confirmed source baseline.
            save_snapshot(self)
        except Exception as exc:
            self.status.setText(f"状态：图层已写入，但变更检测基准保存失败：{exc}")
    return result


_v9._CoreController.save_current_link = _v14_save_current_link
_v9._CoreController.write_planned_links = _v14_write_planned_links


class LinkDesignDock(_v12.LinkDesignDock):
    """v14 dock: v12 cable synchronization plus automatic overlap layout and change detection."""

    def __init__(self, iface, parent=None):
        super().__init__(iface, parent)
        self._change_button = None
        self._install_change_detection_button()

    def _install_change_detection_button(self):
        """Add the compact change-detection action to the top of the dock."""
        container = None
        try:
            container = self.widget()
        except Exception:
            container = None
        container = container or self
        layout = container.layout()
        if layout is None:
            return

        row = QtWidgets.QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 2)
        title = QtWidgets.QLabel("外部数据变更")
        button = QtWidgets.QPushButton("变更检测")
        button.setMinimumHeight(28)
        button.setToolTip("检测 FDT、FAT、Pole Edge 相对于上一次确认状态的变化")
        button.clicked.connect(self._run_change_detection)
        row.addWidget(title)
        row.addStretch(1)
        row.addWidget(button)
        try:
            layout.insertLayout(0, row)
        except Exception:
            layout.addLayout(row)
        self._change_button = button

    def refresh_from_core(self):
        super().refresh_from_core()
        # Keep the action available whenever the Link Design dock is open.
        if self._change_button is not None:
            self._change_button.setEnabled(True)

    def _run_change_detection(self):
        controller = getattr(self, "_controller", None)
        if controller is None:
            QtWidgets.QMessageBox.warning(self, "变更检测", "当前无法访问 Link Design 控制器。")
            return
        designs = getattr(controller, "_designs", []) or []
        if not designs:
            QtWidgets.QMessageBox.information(self, "变更检测", "当前没有已完成的 Link 可检测。")
            return
        try:
            result = detect_changes(controller)
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "变更检测", f"变更检测失败：\n{exc}")
            return
        if result.get("error"):
            QtWidgets.QMessageBox.warning(self, "变更检测", str(result["error"]))
            return
        changes = result.get("changes", []) or []
        if not changes:
            QtWidgets.QMessageBox.information(
                self,
                "变更检测",
                "未发现变化。\n\n当前 FDT、FAT、Pole Edge 与上一次确认状态一致。",
            )
            return
        dialog = ChangeDetectionDialog(controller, result, self)
        dialog.exec_()
        self.refresh_from_core()
