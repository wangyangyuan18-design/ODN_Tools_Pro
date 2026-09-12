# -*- coding: utf-8 -*-
"""Link Design auxiliary feature entry points.

Keeps FAT归杆 and 变更检测 UI wiring outside the canonical Link Design core.
The routing/offset core is not modified by these entry points.
"""

from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import Qt


def _find_layout(layout, target):
    if layout is None:
        return None
    for i in range(layout.count()):
        item = layout.itemAt(i)
        if item.widget() is target:
            return layout
        child = item.layout()
        if child is not None:
            found = _find_layout(child, target)
            if found is not None:
                return found
    return None


def _ensure_summary_row(dock):
    row = getattr(dock, "_link_summary_row", None)
    if row is not None:
        return row
    summary = getattr(dock, "summary", None)
    root = dock.widget() if hasattr(dock, "widget") else None
    root_layout = root.layout() if root is not None else None
    if summary is None or root_layout is None:
        return None
    parent = _find_layout(root_layout, summary)
    if parent is None:
        return None
    for i in range(parent.count()):
        if parent.itemAt(i).widget() is summary:
            insert_at = i
            break
    else:
        return None
    parent.removeWidget(summary)
    row = QtWidgets.QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.addWidget(summary, 1)
    parent.insertLayout(insert_at, row)
    dock._link_summary_row = row
    return row


def _fat_return(dock):
    from .fat_return import fat_return_to_poles
    return fat_return_to_poles(dock)


def _change_detection(dock):
    from .change_detection_adapter import ChangeDetectionDialog, detect_changes
    controller = getattr(dock, "_controller", None)
    if controller is None:
        QtWidgets.QMessageBox.warning(dock, "变更检测", "当前链路设计控制器不可用。")
        return False
    if getattr(controller, "_draw_active", False):
        dock.info.setText("请先保存或退出当前规划，再执行变更检测。")
        return False
    try:
        result = detect_changes(controller)
    except Exception as exc:
        QtWidgets.QMessageBox.warning(dock, "变更检测", f"变更检测失败：\n{exc}")
        return False
    if result.get("error"):
        QtWidgets.QMessageBox.warning(dock, "变更检测", str(result["error"]))
        return False
    changes = result.get("changes", []) or []
    if not changes:
        dock.info.setText("变更检测完成：没有发现 Link 变化。")
        QtWidgets.QMessageBox.information(dock, "变更检测", "变更检测完成。\n\n没有发现需要更新的 Link。")
        return True
    dialog = ChangeDetectionDialog(controller, result, dock)
    accepted = dialog.exec_() == QtWidgets.QDialog.Accepted
    if accepted:
        dock.refresh_from_core()
    return accepted


def install_link_design_feature_buttons(dock):
    """Restore the two Link Design entry buttons without touching core logic."""
    row = _ensure_summary_row(dock)
    if row is None:
        return False

    if getattr(dock, "_fat_return_button", None) is None:
        button = QtWidgets.QPushButton("FAT归杆")
        button.setMinimumHeight(22)
        button.setToolTip("将距离最近 Existing Pole / New Pole 不超过配置距离的 FAT 移到杆上。")
        button.clicked.connect(lambda: _fat_return(dock))
        row.addWidget(button)
        dock._fat_return_button = button

    if getattr(dock, "_change_detection_button", None) is None:
        button = QtWidgets.QPushButton("变更检测")
        button.setMinimumHeight(22)
        button.setToolTip("重新计算全部已保存 Link，列出发生变化的 Link，并确认后整体更新。")
        button.clicked.connect(lambda: _change_detection(dock))
        row.addWidget(button)
        dock._change_detection_button = button

    return True
