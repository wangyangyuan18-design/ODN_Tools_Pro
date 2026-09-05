# -*- coding: utf-8 -*-
"""QGIS plugin entry point for ODN Tools Pro."""

import os


def classFactory(iface):
    """Load the project-driven ODN Tools Pro plugin."""
    from qgis.PyQt.QtGui import QIcon
    from qgis.PyQt.QtWidgets import QAction, QMenu
    from .pole_trace_connect import PoleTraceDialog
    from .overlength_pole import OverlengthPoleDialog
    from .odn_project_manager import OdnProjectManager, initialize_project_manager_context
    from .odn_project_config import open_project_config
    from .odn_project_validation import install_validation_page
    from .odn_project import OdnProjectWizard
    from .odn_project_integration import install_project_creation_integration
    from .link_design_v2 import LinkDesignDialog

    install_validation_page(OdnProjectWizard)
    install_project_creation_integration(OdnProjectWizard)
    initialize_project_manager_context()

    class ODNToolsPro:
        def __init__(self, iface):
            self.iface = iface
            self.plugin_dir = os.path.dirname(os.path.abspath(__file__))
            self.actions = []
            self.menu = None
            self.toolbar = None

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

        def _add(self, text, callback, icon_relpath):
            action = QAction(QIcon(os.path.join(self.plugin_dir, icon_relpath)), text, self.iface.mainWindow())
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
            self._link_design_dialog = LinkDesignDialog(self.iface, self.iface.mainWindow())
            self._link_design_dialog.show()
            self._link_design_dialog.raise_()
            self._link_design_dialog.activateWindow()

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
