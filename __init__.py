# -*- coding: utf-8 -*-
"""QGIS plugin entry point for ODN Tools Pro."""
import os


def _install_offset_core_compatibility():
    """Restore the public Offset Core entry point used by Link Design.

    The authoritative geometry remains in cable_offset_core.build_offset_designs().
    This shim only restores the historical apply_offset_layout() API; it does not
    introduce another geometry engine or alter lane/corner rules.
    """
    from . import cable_offset_core as offset_core

    if hasattr(offset_core, "apply_offset_layout"):
        return

    def apply_offset_layout(
        designs,
        distribution_layer,
        edge_layer,
        spacing=offset_core.DEFAULT_SPACING_M,
        control_distance_m=offset_core.DEFAULT_CONTROL_M,
        fat_layer=None,
        fat_max_distance_m=offset_core.DEFAULT_FAT_MAX_DISTANCE_M,
    ):
        spacing = max(0.01, float(spacing))
        control_distance_m = max(0.01, float(control_distance_m))
        offset_core.save_settings(spacing, control_distance_m)
        result = offset_core.build_offset_designs(
            designs,
            distribution_layer,
            fat_layer=fat_layer,
            edge_layer=edge_layer,
        )
        stats = result.get("fat_stats") or {}
        return {
            "changed_designs": len(result.get("designs") or []),
            "changed_indices": list(range(len(result.get("designs") or []))),
            "spacing_m": spacing,
            "extra_length_m": 0.0,
            "version": 26,
            "work_crs": result.get("work_crs").authid() if result.get("work_crs") else "",
            "lane_allocator": "OffsetCore",
            "priority": result.get("ordered") or [],
            "slot_map": {str(k): int(v) for k, v in (result.get("slots") or {}).items()},
            "corner_geometry": "route_lane_corner_decision_D_E_F",
            "corner_decisions": {},
            "fanout_control_distance_m": control_distance_m,
            "fat_moves": result.get("moves") or {},
            "fat_total": int(stats.get("total", 0)),
            "fat_skipped": int(stats.get("skipped", 0)),
            "fat_corner": int(stats.get("corner", 0)),
            "fat_straight": int(stats.get("straight", 0)),
            "fat_endpoint_updates": int(result.get("touched", 0)),
            "fat_max_distance_m": float(fat_max_distance_m),
        }

    offset_core.apply_offset_layout = apply_offset_layout


def classFactory(iface):
    from qgis.PyQt.QtGui import QIcon
    from qgis.PyQt.QtWidgets import QAction, QMenu
    from qgis.PyQt.QtCore import Qt
    from .pole_trace_connect import PoleTraceDialog
    from .overlength_pole import OverlengthPoleDialog
    from .odn_project_manager import OdnProjectManager, initialize_project_manager_context
    from .odn_project_config import open_project_config
    from .odn_project_validation import install_validation_page
    from .odn_project import OdnProjectWizard
    from .odn_project_integration import install_project_creation_integration

    # Must run before link_design_core imports cable_offset_core.apply_offset_layout.
    _install_offset_core_compatibility()

    from .link_design import LinkDesignDock
    from .fat_return import install_fat_return_button
    from .link_design_features import install_link_design_feature_buttons
    from .plugin_undo import undo_last
    from .odn_link_rules import install_project_config_defaults

    install_validation_page(OdnProjectWizard)
    install_project_creation_integration(OdnProjectWizard)
    initialize_project_manager_context()
    install_project_config_defaults()

    class ODNToolsPro:
        def __init__(self, iface):
            self.iface = iface
            self.plugin_dir = os.path.dirname(os.path.abspath(__file__))
            self.actions = []
            self.menu = None
            self.toolbar = None
            self._link_design_dock = None

        def initGui(self):
            main_window = self.iface.mainWindow()
            self.menu = QMenu("ODN Tools Pro", main_window)
            main_window.menuBar().addMenu(self.menu)
            self.toolbar = self.iface.addToolBar("ODN Tools Pro")
            self.toolbar.setObjectName("ODNToolsProToolbar")
            self._add("项目管理", self.project_manager, "icons/project_manager.svg")
            self._add("项目配置", self.project_config, "icons/project_config.svg")
            self._add("杆路轨迹自动连线", self.pole_trace_connect, "icons/pole_trace.svg")
            self._add("超距增点", self.overlength_pole, "icons/overlength_pole.svg")
            self._add("链路设计", self.link_design, "icons/link_design.svg")
            self._add_undo()

        def _add(self, text, callback, icon_relpath):
            action = QAction(QIcon(os.path.join(self.plugin_dir, icon_relpath)), text, self.iface.mainWindow())
            action.triggered.connect(callback)
            self.menu.addAction(action)
            self.toolbar.addAction(action)
            self.actions.append(action)

        def _add_undo(self):
            action = QAction("回退", self.iface.mainWindow())
            action.setToolTip("回退 ODN Tools Pro 最近一次已记录的操作")
            action.setShortcut("Ctrl+Shift+Z")
            action.triggered.connect(lambda: self._undo_last())
            self.menu.addAction(action)
            self.toolbar.addAction(action)
            self.actions.append(action)
            self._undo_action = action

        def _undo_last(self):
            if not undo_last(self.iface.mainWindow()):
                try:
                    self.iface.messageBar().pushWarning("ODN Tools Pro", "没有可回退的插件操作。")
                except Exception:
                    pass

        def project_manager(self):
            OdnProjectManager(self.iface, self.iface.mainWindow()).exec_()

        def project_config(self):
            open_project_config(self.iface, self.iface.mainWindow())

        def pole_trace_connect(self):
            PoleTraceDialog(self.iface, self.iface.mainWindow()).exec_()

        def overlength_pole(self):
            OverlengthPoleDialog(self.iface, self.iface.mainWindow()).exec_()

        def link_design(self):
            if self._link_design_dock is None:
                self._link_design_dock = LinkDesignDock(self.iface, self.iface.mainWindow())
                install_fat_return_button(self._link_design_dock)
                install_link_design_feature_buttons(self._link_design_dock)
                self.iface.addDockWidget(Qt.LeftDockWidgetArea, self._link_design_dock)
            else:
                install_link_design_feature_buttons(self._link_design_dock)
            self._link_design_dock.show()
            self._link_design_dock.raise_()
            self._link_design_dock.activateWindow()
            try:
                self._link_design_dock._overlay.show()
                self._link_design_dock._overlay.raise_()
            except Exception:
                pass

        def unload(self):
            if self._link_design_dock is not None:
                try:
                    self._link_design_dock.close()
                except Exception:
                    pass
                self._link_design_dock = None
            for action in self.actions:
                try:
                    action.deleteLater()
                except Exception:
                    pass
            self.actions.clear()
            if self.toolbar is not None:
                try:
                    self.iface.mainWindow().removeToolBar(self.toolbar)
                except Exception:
                    pass
                self.toolbar = None
            if self.menu is not None:
                try:
                    self.iface.mainWindow().menuBar().removeAction(self.menu.menuAction())
                    self.menu.deleteLater()
                except Exception:
                    pass
                self.menu = None

    return ODNToolsPro(iface)
