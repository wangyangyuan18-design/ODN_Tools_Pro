# -*- coding: utf-8 -*-
"""Link Design v14: automatic multi-cable Pole Edge offset layout."""

from qgis.PyQt.QtCore import QSettings
from qgis.PyQt import QtWidgets

from . import link_design_v12 as _v12
from . import link_design_v9 as _v9
from .cable_offset_layout_v2 import apply_layout_to_designs


_ORIGINAL_WRITE = _v9._CoreController.write_planned_links
SPACING_KEY = "ODNToolsPro/CableOffsetLayout/spacing_m"
DEFAULT_SPACING_M = 0.5


def _spacing_m():
    try:
        value = float(QSettings().value(SPACING_KEY, DEFAULT_SPACING_M))
    except (TypeError, ValueError):
        value = DEFAULT_SPACING_M
    return value if value > 0 else DEFAULT_SPACING_M


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

    return _ORIGINAL_WRITE(self)


_v9._CoreController.write_planned_links = _v14_write_planned_links


class LinkDesignDock(_v12.LinkDesignDock):
    """v14 dock: v12 cable synchronization plus automatic overlap layout."""

    pass
