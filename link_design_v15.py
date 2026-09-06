# -*- coding: utf-8 -*-
"""Link Design v15 UI/state refinements over v14."""

from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import QEvent, QSettings, Qt
from qgis.PyQt.QtGui import QColor
from qgis.core import (
    QgsCoordinateTransform,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsWkbTypes,
)
from qgis.gui import QgsRubberBand

from . import link_design_v14 as _v14
from . import link_design_v11 as _v11
from . import odn_project_context as context

SHOW_COMPLETED_FAT_KEY = "ODNToolsPro/LinkDesign/show_completed_fat_marks"

_original_refresh_planning_state = _v11.LinkDesignMapToolV11.refresh_planning_state


def _v15_refresh_planning_state(self):
    _original_refresh_planning_state(self)
    if getattr(self.controller, "_show_completed_fat_marks", True):
        return
    try:
        self._remove_bands(self._planned_fat_bands)
    except Exception:
        pass


_v11.LinkDesignMapToolV11.refresh_planning_state = _v15_refresh_planning_state


class LinkDesignDock(_v14.LinkDesignDock):
    """Active Link Design dock with minimal UI/state refinements."""

    def __init__(self, iface, parent=None):
        self._show_completed_fat_marks = bool(
            QSettings().value(SHOW_COMPLETED_FAT_KEY, True, type=bool)
        )
        self._selected_fat_bands = []
        super().__init__(iface, parent)
        self._install_ui_refinements()

    def _clear_selected_fat_highlights(self):
        for band in list(self._selected_fat_bands):
            try:
                self.iface.mapCanvas().scene().removeItem(band)
            except Exception:
                pass
        self._selected_fat_bands = []

    def _highlight_selected_fats(self, indexes):
        self._clear_selected_fat_highlights()
        if not indexes:
            return
        controller = self._controller
        payload = controller._v9_payload()
        fat_layer = context.project_layer(payload, "FAT")
        if fat_layer is None:
            return
        canvas = self.iface.mapCanvas()
        dst = canvas.mapSettings().destinationCrs()
        transform = None
        if fat_layer.crs() != dst:
            try:
                transform = QgsCoordinateTransform(
                    fat_layer.crs(), dst, QgsProject.instance().transformContext()
                )
            except Exception:
                return

        fat_ids = set()
        for index in indexes:
            if index < 0 or index >= len(controller._designs):
                continue
            for item in controller._designs[index].get("sequence_ids", []) or []:
                if len(item) >= 2 and str(item[0]) == "FAT":
                    try:
                        fat_ids.add(int(item[1]))
                    except (TypeError, ValueError):
                        pass

        for fid in sorted(fat_ids):
            try:
                feature = fat_layer.getFeature(int(fid))
                if not feature.isValid() or feature.geometry().isEmpty():
                    continue
                point = QgsPointXY(feature.geometry().asPoint())
                if transform is not None:
                    point = transform.transform(point)
            except Exception:
                continue
            band = QgsRubberBand(canvas, QgsWkbTypes.PointGeometry)
            band.setColor(QColor(255, 165, 0, 235))
            band.setWidth(4)
            band.setIcon(QgsRubberBand.ICON_CIRCLE)
            band.setIconSize(17)
            band.setToGeometry(QgsGeometry.fromPointXY(point), dst)
            self._selected_fat_bands.append(band)

    def _install_ui_refinements(self):
        self._replace_summary_row()
        self.tree.viewport().installEventFilter(self)
        try:
            self._overlay.exit.clicked.disconnect(self._controller.exit_design)
        except Exception:
            pass
        self._overlay.exit.clicked.connect(self._exit_design)

    def _replace_summary_row(self):
        root = self.widget()
        layout = root.layout() if root is not None else None
        if layout is None or not hasattr(self, "summary"):
            return
        old_summary = self.summary
        try:
            index = layout.indexOf(old_summary)
        except Exception:
            index = -1
        if index < 0:
            return
        layout.removeWidget(old_summary)
        old_summary.hide()
        row = QtWidgets.QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        self.show_fat_marks = QtWidgets.QCheckBox()
        self.show_fat_marks.setChecked(self._show_completed_fat_marks)
        self.show_fat_marks.setToolTip("显示/隐藏已完成 FAT 的灰色标记")
        self.show_fat_marks.setFixedWidth(18)
        self.show_fat_marks.setFixedHeight(18)
        self.summary_link_label = QtWidgets.QLabel()
        self.summary_fat_label = QtWidgets.QLabel()
        row.addWidget(self.summary_link_label)
        row.addSpacing(8)
        row.addWidget(self.show_fat_marks)
        row.addWidget(self.summary_fat_label)
        row.addStretch(1)
        layout.insertLayout(index, row)
        self.show_fat_marks.toggled.connect(self._toggle_completed_fat_marks)

    def _toggle_completed_fat_marks(self, checked):
        self._show_completed_fat_marks = bool(checked)
        QSettings().setValue(SHOW_COMPLETED_FAT_KEY, self._show_completed_fat_marks)
        QSettings().sync()
        tool = getattr(self._controller, "_tool", None)
        if tool is not None:
            try:
                tool.refresh_planning_state()
            except Exception:
                pass
        self.iface.mapCanvas().refresh()

    def eventFilter(self, obj, event):
        viewport = getattr(self.tree, "viewport", lambda: None)()
        if obj is viewport and event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
            # QGIS 3.40 uses the Qt5-style QMouseEvent API here.
            try:
                pos = event.pos()
            except AttributeError:
                pos = event.position().toPoint()
            if self.tree.itemAt(pos) is None:
                self.tree.clearSelection()
                self.tree.setCurrentItem(None)
                self._controller._clear_saved_bands()
                self._clear_selected_fat_highlights()
                self.info.setText("—")
                return True
        return super().eventFilter(obj, event)

    def _tree_selection_changed(self):
        super()._tree_selection_changed()
        self._highlight_selected_fats(self._tree_selected_links())

    def _has_unfinished_design(self):
        c = self._controller
        return bool(
            getattr(c, "_draw_active", False)
            or getattr(c, "_sequence", None)
            or getattr(c, "_editing_index", None) is not None
        )

    def _exit_design(self):
        c = self._controller
        if self._has_unfinished_design():
            answer = QtWidgets.QMessageBox.question(
                self,
                "退出设计",
                "当前有正在规划或修改中的 Link。\n\n是否保存当前规划后退出？",
                QtWidgets.QMessageBox.Save | QtWidgets.QMessageBox.Discard | QtWidgets.QMessageBox.Cancel,
                QtWidgets.QMessageBox.Save,
            )
            if answer == QtWidgets.QMessageBox.Cancel:
                return
            if answer == QtWidgets.QMessageBox.Save:
                try:
                    result = c.save_current_link()
                except Exception as exc:
                    QtWidgets.QMessageBox.warning(self, "退出设计", f"保存失败，未退出设计：\n{exc}")
                    return
                if not result:
                    QtWidgets.QMessageBox.warning(self, "退出设计", "当前规划保存失败，未退出设计。")
                    return

        try:
            tool = getattr(c, "_tool", None)
            if tool is not None:
                try:
                    tool.clear_preview_only()
                except Exception:
                    pass
                try:
                    tool._clear_planning_state_bands()
                except Exception:
                    pass
            c._stop_tool()
        except Exception:
            pass
        self._clear_selected_fat_highlights()
        c._draw_active = False
        c._sequence = []
        c._current_fdt = None
        c._current_fdt_id = None
        c._current_link = None
        c._hover_segment = None
        c._hover_distance = 0.0
        try:
            c._editing_index = None
            c._editing_written_index = None
        except Exception:
            pass
        try:
            c._persist_state()
            c._refresh_ui()
        except Exception:
            pass
        try:
            self._overlay.hide()
        except Exception:
            pass
        self.iface.mapCanvas().refresh()

    def refresh_from_core(self):
        super().refresh_from_core()
        c = self._controller
        total_fats = 0
        try:
            total_fats = _v14._v9._v2._total_fats(c)
        except Exception:
            pass
        used_fats = set()
        for design in getattr(c, "_designs", []) or []:
            for node in design.get("nodes", []) or []:
                try:
                    used_fats.add(int(node[0]))
                except Exception:
                    pass
        if hasattr(self, "summary_link_label"):
            self.summary_link_label.setText(f"已完成Link:{len(c._designs)}")
            self.summary_fat_label.setText(f"已完成FAT:{len(used_fats)}/{total_fats}")
        elif hasattr(self, "summary"):
            self.summary.setText(f"已完成Link:{len(c._designs)}    已完成FAT:{len(used_fats)}/{total_fats}")
        try:
            if getattr(c, "_draw_active", False) or getattr(c, "_sequence", None):
                self._overlay.show()
                self._overlay.raise_()
        except Exception:
            pass
