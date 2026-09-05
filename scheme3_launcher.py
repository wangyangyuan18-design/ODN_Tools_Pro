# -*- coding: utf-8 -*-
"""Direct Link Design launcher using the active ODN Project."""

from qgis.PyQt import QtWidgets
from qgis.core import QgsWkbTypes

from . import odn_project_context as context


def _layer(payload, role):
    return context.project_layer(payload, role)


def _project_layers(dialog):
    payload = getattr(dialog, "_odn_project_payload", None) or {}
    fdt = _layer(payload, "FDT")
    fat = _layer(payload, "FAT")
    existing = _layer(payload, "Existing Pole")
    new = _layer(payload, "New Pole")
    edge = _layer(payload, "Pole Edge")
    fats = [fat] if fat is not None else []
    poles = [x for x in (existing, new) if x is not None]
    return fdt, fats, poles, edge


def _parameter_int(payload, key, default, minimum=1, maximum=1000):
    value = (payload or {}).get("parameters", {}).get(key, default)
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def start_link_design(iface, parent=None):
    """Open Link Design directly from the globally active ODN Project."""
    payload = context.require_project(parent or iface.mainWindow(), "链路设计")
    if payload is None:
        return None

    from .scheme3_manual_link_planner import Scheme3Dialog

    dialog = None
    try:
        dialog = Scheme3Dialog(iface, parent or iface.mainWindow())
        dialog._odn_project_path = context.current_path()
        dialog._odn_project_payload = payload

        fdt, fats, poles, edge = _project_layers(dialog)
        missing = []
        if fdt is None or QgsWkbTypes.geometryType(fdt.wkbType()) != QgsWkbTypes.PointGeometry:
            missing.append("FDT")
        if not fats:
            missing.append("FAT")
        if edge is None or QgsWkbTypes.geometryType(edge.wkbType()) != QgsWkbTypes.LineGeometry:
            missing.append("Pole Edge")
        if missing:
            QtWidgets.QMessageBox.warning(
                parent or iface.mainWindow(),
                "链路设计",
                "当前项目缺少链路设计所需图层：\n\n"
                + "\n".join(f"• {role}" for role in missing)
                + "\n\n请先在【项目管理 → 项目配置】中添加/修正这些图层。"
            )
            dialog.deleteLater()
            return None

        dialog._layers = lambda: _project_layers(dialog)
        dialog.fdt_combo.clear()
        dialog.link_combo.clear()
        max_links = _parameter_int(payload, "fdt_max_links", 8, 1, 1000)
        dialog.link_combo.addItems([f"L{i}" for i in range(1, max_links + 1)])
        dialog.fdt_combo.addItem(fdt.name(), fdt.id())
        dialog._refresh_tree()
        dialog.fdt_combo.setCurrentIndex(0)
        dialog._selection_changed()

        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        return dialog
    except Exception as exc:
        if dialog is not None:
            dialog.deleteLater()
        QtWidgets.QMessageBox.critical(
            parent or iface.mainWindow(),
            "链路设计",
            f"打开链路设计失败：\n{exc}"
        )
        return None
