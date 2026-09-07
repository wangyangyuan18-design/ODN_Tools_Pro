# -*- coding: utf-8 -*-
"""Link Design v14: Link/Distribution Cable synchronization and change detection."""

from qgis.PyQt.QtCore import QSettings
from qgis.PyQt import QtWidgets

from . import link_design_v12 as _v12
from . import link_design_v9 as _v9
from .change_detection_adapter import ChangeDetectionDialog, detect_changes, save_snapshot

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
            self.status.setText(f"状态：Link 已保存，但变更检测基准保存失败：{exc}")
    return result


def _v14_write_planned_links(self):
    """Normal write: synchronize the saved Link topology into Distribution Cable.

    Offset geometry is intentionally NOT applied here. The dedicated
    "偏移并写入图层" action in v15 performs that operation explicitly.
    """
    result = _ORIGINAL_WRITE(self)
    if result:
        try:
            save_snapshot(self)
        except Exception as exc:
            self.status.setText(f"状态：图层已写入，但变更检测基准保存失败：{exc}")
    return result


_v9._CoreController.save_current_link = _v14_save_current_link
_v9._CoreController.write_planned_links = _v14_write_planned_links


class LinkDesignDock(_v12.LinkDesignDock):
    """v14 dock: v12 synchronization plus change detection."""

    def __init__(self, iface, parent=None):
        self._change_button = None
        super().__init__(iface, parent)
        self._install_change_detection_button()

    def _install_change_detection_button(self):
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
            controller._engine = None
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
