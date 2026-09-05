# -*- coding: utf-8 -*-
"""QGIS plugin entry point for ODN Tools Pro."""

import os


def classFactory(iface):
    """Load the rebuilt ODN Tools Pro plugin."""
    from .pole_trace_connect import PoleTraceDialog
    from .overlength_pole import OverlengthPoleDialog
    from .odn_project_manager import OdnProjectManager, initialize_project_manager_context
    from .odn_project_config import open_project_config, OdnProjectConfigDialog
    from .odn_project_validation import install_validation_page
    from .odn_project import OdnProjectWizard
    from .odn_project_integration import install_project_creation_integration
    from .odn_project_config_fix import install_project_config_fix
    from .scheme3_policy import install_scheme3_policy
    from .scheme3_route_preview_fix import install_scheme3_route_preview_fix
    from .scheme3_manual_link_planner import Scheme3Dialog, Scheme3Engine, Scheme3MapTool
    from .scheme3_launcher import start_link_design
    from .scheme3_project_ui import install_project_ui_policy

    install_validation_page(OdnProjectWizard)
    install_project_creation_integration(OdnProjectWizard)
    install_project_config_fix(OdnProjectConfigDialog)
    install_scheme3_policy(Scheme3Dialog)
    install_scheme3_route_preview_fix(Scheme3Dialog, Scheme3Engine, Scheme3MapTool)
    install_project_ui_policy(Scheme3Dialog)
    initialize_project_manager_context()

    class ODNToolsPro:
        """Main plugin controller for the rebuilt ODN Tools Pro."""

        def __init__(self, iface):
            self.iface = iface
            self.plugin_dir = os.path.dirname(os.path.abspath(__file__))
            self.actions = []
            self.menu = None
            self.toolbar = None

        def initGui(self):
            from qgis.PyQt.QtWidgets import QAction, QMenu

            main_window = self.iface.mainWindow()
            self.menu = QMenu("ODN Tools Pro", main_window)
            main_window.menuBar().addMenu(self.menu)
            self.toolbar = self.iface.addToolBar("ODN Tools Pro")
            self.toolbar.setObjectName("ODNToolsProToolbar")

            self._add("项目管理", self.project_manager)
            self._add("项目配置", self.project_config)
            self._add("杆路轨迹自动连线", self.pole_trace_connect)
            self._add("超距增点", self.overlength_pole)
            self._add("链路设计", self.link_design)

        def _add(self, text, callback):
            from qgis.PyQt.QtWidgets import QAction
            action = QAction(text, self.iface.mainWindow())
            action.triggered.connect(callback)
            self.menu.addAction(action)
            self.toolbar.addAction(action)
            self.actions.append(action)

        def project_manager(self):
            OdnProjectManager(self.iface, self.iface.mainWindow()).exec_()

        def project_config(self):
            open_project_config(self.iface, self.iface.mainWindow())

        def pole_trace_connect(self):
            PoleTraceDialog(self.iface, self.iface.mainWindow()).exec_()

        def overlength_pole(self):
            OverlengthPoleDialog(self.iface, self.iface.mainWindow()).exec_()

        def link_design(self):
            self._scheme3_dialog = start_link_design(self.iface, self.iface.mainWindow())

        def unload(self):
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
