# -*- coding: utf-8 -*-
"""Bridge Scheme 3 to an ODN Project's semantic layer registry.

Scheme 3 historically discovered layers by display-name keywords.  An ODN
Project must instead be authoritative: use the stored QGIS layer IDs and
roles, while retaining the existing Scheme 3 engine/UI implementation.
"""

from qgis.PyQt import QtWidgets
from qgis.core import QgsProject, QgsMapLayerType, QgsWkbTypes


def _layer(payload, role):
    entry = (payload or {}).get("layer_registry", {}).get(role, {})
    return QgsProject.instance().mapLayer(entry.get("layer_id", ""))


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


def _load_project_candidates(dialog):
    payload = getattr(dialog, "_odn_project_payload", None) or {}
    fdt = _layer(payload, "FDT")
    dialog.fdt_combo.clear()
    dialog.link_combo.clear()
    dialog.link_combo.addItems([f"L{i}" for i in range(1, 9)])
    if fdt is not None and QgsWkbTypes.geometryType(fdt.wkbType()) == QgsWkbTypes.PointGeometry:
        dialog.fdt_combo.addItem(fdt.name(), fdt.id())
    dialog._refresh_tree()


def _start_scheme3(self):
    try:
        from .scheme3_manual_link_planner import Scheme3Dialog
        dialog = Scheme3Dialog(self.iface, self.iface.mainWindow())
        dialog._odn_project_path = self.project_path
        dialog._odn_project_payload = self.payload

        fdt, fats, poles, edge = _project_layers(dialog)
        if fdt is None:
            raise RuntimeError("Project 中没有有效的 FDT 图层绑定。")
        if not fats:
            raise RuntimeError("Project 中没有有效的 FAT 图层绑定。")
        if edge is None:
            raise RuntimeError("Project 中没有有效的 Pole Edge 图层绑定。")

        dialog._layers = lambda: _project_layers(dialog)
        _load_project_candidates(dialog)
        dialog.fdt_combo.setCurrentIndex(0)
        dialog._selection_changed()

        self.scheme3_dialog = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
    except Exception as exc:
        QtWidgets.QMessageBox.critical(self, "方案3", f"打开方案3失败：\n{exc}")


def install_workspace_scheme3_adapter(workspace_class):
    """Make the workspace launch Scheme 3 with Project-bound layers."""
    workspace_class._start_scheme3 = _start_scheme3
