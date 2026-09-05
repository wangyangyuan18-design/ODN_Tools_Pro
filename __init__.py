# -*- coding: utf-8 -*-
"""QGIS plugin package entry point for ODN Tools Pro."""


def classFactory(iface):
    """Load the ODN Tools Pro plugin for QGIS."""
    from . import connection_point_engine
    from . import connection_point_engine_v4
    connection_point_engine.run_connection_point_naming_v2 = connection_point_engine_v4.run_connection_point_naming_v2
    from .site_co_design_impl import SiteCoDesign
    from .pole_trace_connect import PoleTraceDialog
    from .overlength_pole import OverlengthPoleDialog
    from .odn2_planner import Odn2PlannerDialog
    from .scheme3_manual_link_planner import Scheme3Dialog, Scheme3Engine, Scheme3MapTool
    from .odn_project import OdnProjectWizard
    from .odn_project_manager import OdnProjectManager, initialize_project_manager_context
    from .odn_project_config import open_project_config, OdnProjectConfigDialog
    from .odn_project_validation import install_validation_page
    from .scheme3_policy import install_scheme3_policy
    from .odn_project_integration import install_project_creation_integration
    from .odn_project_config_fix import install_project_config_fix
    from .scheme3_route_preview_fix import install_scheme3_route_preview_fix
    from .scheme3_launcher import start_link_design
    from .scheme3_project_ui import install_project_ui_policy
    from . import odn_project_context as project_context

    install_validation_page(OdnProjectWizard)
    install_scheme3_policy(Scheme3Dialog)
    install_project_creation_integration(OdnProjectWizard)
    install_project_config_fix(OdnProjectConfigDialog)
    install_scheme3_route_preview_fix(Scheme3Dialog, Scheme3Engine, Scheme3MapTool)
    install_project_ui_policy(Scheme3Dialog)
    initialize_project_manager_context()
    import os

    class ODNToolsPro(SiteCoDesign):
        """Main QGIS plugin controller with ODN planning tools."""

        def initGui(self):
            super().initGui()
            main_window = self.iface.mainWindow()
            icon_path = os.path.join(self.plugin_dir, 'poleTraceConnect.svg')
            self.add_action(icon_path, self.tr('项目管理'), self.project_manager, parent=main_window)
            self.add_action(icon_path, self.tr('项目配置'), self.project_config, parent=main_window)
            self.add_action(icon_path, self.tr('杆路轨迹自动连线'), self.pole_trace_connect, parent=main_window)
            self.add_action(icon_path, self.tr('超距增点'), self.overlength_pole, parent=main_window)
            self.add_action(icon_path, self.tr('ODN 网络规划'), self.fdt_planner, parent=main_window)
            self.add_action(icon_path, self.tr('链路设计'), self.link_design, parent=main_window)

        def project_manager(self):
            dialog = OdnProjectManager(self.iface, self.iface.mainWindow())
            dialog.exec_()

        def project_config(self):
            if not project_context.require_project(self.iface.mainWindow(), '项目配置'):
                return
            open_project_config(self.iface, self.iface.mainWindow())

        def new_odn_project(self):
            # Backwards-compatible programmatic entry; the visible UI entry is 项目管理.
            self._odn_project_dialog = OdnProjectWizard(self.iface, self.iface.mainWindow())
            self._odn_project_dialog.exec_()

        def pole_trace_connect(self):
            PoleTraceDialog(self.iface, self.iface.mainWindow()).exec_()

        def overlength_pole(self):
            OverlengthPoleDialog(self.iface, self.iface.mainWindow()).exec_()

        def fdt_planner(self):
            Odn2PlannerDialog(self.iface, self.iface.mainWindow()).exec_()

        def link_design(self):
            self._scheme3_dialog = start_link_design(self.iface, self.iface.mainWindow())

    return ODNToolsPro(iface)
