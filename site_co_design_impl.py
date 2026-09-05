# -*- coding: utf-8 -*-
"""QGIS plugin entry/controller for ODN Tools Pro."""

import os

from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import QSettings, QTranslator, QCoreApplication
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QMenu

from . import site_co_design_library as _legacy_library
from .validation_optimized import validation_run as _optimized_validation_run

_legacy_library.validation_run = _optimized_validation_run

from .site_co_design_dialog import (
    CableNamingDialog,
    CableSplitDialog,
    CableValidationDialog,
)
from .connection_point_dialog import ConnectionPointDialog
from .connection_point_engine import run_connection_point_naming_v2
from .site_co_design_library import dialog_open


def _connection_point_dialog_on_ok(self):
    """Run the multi-node naming engine, optionally naming Cable features too."""
    fdt_layer = self._layer_from_combo(self.fdtLayerCombo)
    line_layer = self._layer_from_combo(self.lineLayerCombo)
    nodes = self._node_payload()

    def critical(message):
        try:
            if self.iface is not None and hasattr(self.iface, 'messageBar'):
                self.iface.messageBar().pushCritical('ODN Tools Pro', message)
            else:
                QtWidgets.QMessageBox.critical(self, 'ODN Tools Pro', message)
        except Exception:
            QtWidgets.QMessageBox.critical(self, 'ODN Tools Pro', message)

    if fdt_layer is None:
        critical('请选择汇聚点图层')
        return
    if not self.fdtFieldCombo.currentText().strip():
        critical('请选择汇聚点字段')
        return
    if line_layer is None:
        critical('请选择连接汇聚点与接入点图层')
        return
    if not nodes:
        critical('至少勾选一个节点图层（FAT / CL / BB）')
        return

    if self.addRadio.isChecked() and not self.addFieldEdit.text().strip():
        critical('请输入新增字段名称')
        return
    if self.modRadio.isChecked() and not self.modFieldCombo.currentText().strip():
        critical('请选择修改字段')
        return

    line_name_enabled = self.lineNameEnable.isChecked()
    line_name_field = self.lineNameFieldCombo.currentText().strip()
    if line_name_enabled:
        if not line_name_field:
            critical('已勾选“同时命名连接线”，请选择连接线输出字段')
            return
        if line_layer.fields().indexOf(line_name_field) < 0:
            critical(f'连接线图层缺少输出字段：{line_name_field}')
            return

    params = {
        'line_layer_name': line_layer.name(),
        'line_layer_id': line_layer.id(),
        'conv_point_layer_name': fdt_layer.name(),
        'conv_point_layer_id': fdt_layer.id(),
        'conv_point_layer_field_name': self.fdtFieldCombo.currentText(),
        'point_name_sort_enum': 'fromConv' if self.nameOrderCombo.currentText().startswith('从汇聚') else 'toConv',
        'point_name_directions_enum': 'clockwise' if self.sortDirCombo.currentText() == '顺时针' else 'counter',
        'l_prefix': self.lPrefixEdit.text(),
        'l_suffix': self.lSuffixCombo.currentText(),
        'prefix': self.fdtPrefixEdit.text(),
        'nodes': nodes,
        'fat_enabled': 'FAT' in nodes,
        'fat_layer_name': nodes.get('FAT', {}).get('layer_name'),
        'fat_layer_id': nodes.get('FAT', {}).get('layer_id'),
        'cl_enabled': 'CL' in nodes,
        'cl_layer_name': nodes.get('CL', {}).get('layer_name'),
        'cl_layer_id': nodes.get('CL', {}).get('layer_id'),
        'bb_enabled': 'BB' in nodes,
        'bb_layer_name': nodes.get('BB', {}).get('layer_name'),
        'bb_layer_id': nodes.get('BB', {}).get('layer_id'),
        'fat_prefix': self.fatPrefixEdit.text(),
        'fat_suffix': self.fatSuffixCombo.currentText(),
        'cl_prefix': self.clPrefixEdit.text(),
        'cl_suffix': self.clSuffixCombo.currentText(),
        'bb_prefix': self.bbPrefixEdit.text(),
        'bb_auto_type': self.bbAutoCheck.isChecked(),
        'data_write_mode': 'addAttr' if self.addRadio.isChecked() else 'modifyAttr',
        'add_attr_name': self.addFieldEdit.text().strip(),
        'modify_attr_name': self.modFieldCombo.currentText().strip(),
        'line_name_enabled': line_name_enabled,
        'line_name_direction': 'fromConv' if self.lineNameDirectionCombo.currentText().startswith('从汇聚') else 'toConv',
        'line_name_field': line_name_field,
    }

    save_data = dict(params)
    save_data.update({
        'fdt_layer': fdt_layer.id(),
        'fdt_field': self.fdtFieldCombo.currentText(),
        'line_layer': line_layer.id(),
        'name_order': self.nameOrderCombo.currentText(),
        'sort_dir': self.sortDirCombo.currentText(),
        'fat_layer': nodes.get('FAT', {}).get('layer_id'),
        'cl_layer': nodes.get('CL', {}).get('layer_id'),
        'bb_layer': nodes.get('BB', {}).get('layer_id'),
        'add_mode': self.addRadio.isChecked(),
        'add_field': self.addFieldEdit.text(),
        'modify_field': self.modFieldCombo.currentText(),
        'line_name_enabled': line_name_enabled,
        'line_name_direction': self.lineNameDirectionCombo.currentText(),
        'line_name_field': line_name_field,
    })
    from qgis.core import QgsProject
    QgsProject.instance().writeEntry('site_co_design', 'connectionPointNamingV2', save_data)

    try:
        # Keep the mature FAT-only path when cable sync is disabled.
        if params['line_name_enabled'] or params['cl_enabled'] or params['bb_enabled']:
            ok = run_connection_point_naming_v2(params, iface=self.iface)
        else:
            from .site_co_design_library import connection_point_naming_run
            legacy = {
                'line_layer_name': params['line_layer_name'],
                'conv_point_layer_name': params['conv_point_layer_name'],
                'conv_point_layer_field_name': params['conv_point_layer_field_name'],
                'norm_point_layer_name': params['fat_layer_name'],
                'point_name_sort_enum': params['point_name_sort_enum'],
                'point_name_directions_enum': params['point_name_directions_enum'],
                'variableL': params['l_prefix'].strip('_') or 'L',
                'variableS': params['fat_prefix'].strip('_') or 'S',
                'variableLSuf': params['l_suffix'] or 'num',
                'variableSSuf': params['fat_suffix'] or 'num',
                'prefix': params['prefix'],
                'data_write_mode': params['data_write_mode'],
                'add_attr_name': params['add_attr_name'],
                'modify_attr_name': params['modify_attr_name'],
            }
            ok = connection_point_naming_run(legacy, iface=self.iface)
    except Exception as exc:
        critical(f'接入点命名运行失败：{exc}')
        return

    if ok is not False:
        self.accept()


ConnectionPointDialog._on_ok = _connection_point_dialog_on_ok


class SiteCoDesign:
    """Main QGIS plugin controller."""

    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(os.path.abspath(__file__))
        self.actions = []
        self.site_co_design_menu = None
        self.site_co_design_toolbar = None
        self.translator = None
        self._load_translator()

    def _load_translator(self):
        try:
            locale = QSettings().value('locale/userLocale', 'en') or 'en'
            locale = str(locale)[:2]
        except Exception:
            locale = 'en'
        locale_path = os.path.join(self.plugin_dir, 'i18n', f'SiteCoDesign_{locale}.qm')
        if not os.path.exists(locale_path):
            return
        try:
            translator = QTranslator()
            if translator.load(locale_path):
                QCoreApplication.installTranslator(translator)
                self.translator = translator
        except Exception:
            self.translator = None

    def tr(self, message):
        return QCoreApplication.translate('ODNToolsPro', message)

    def add_action(self, icon_path, text, callback, enabled_flag=True,
                   add_to_menu=True, add_to_toolbar=True, status_tip=None,
                   whats_this=None, parent=None):
        action = QAction(QIcon(icon_path), text, parent)
        action.setEnabled(enabled_flag)
        if status_tip:
            action.setStatusTip(status_tip)
        if whats_this:
            action.setWhatsThis(whats_this)
        action.triggered.connect(callback)
        if add_to_toolbar and self.site_co_design_toolbar is not None:
            self.site_co_design_toolbar.addAction(action)
        if add_to_menu and self.site_co_design_menu is not None:
            self.site_co_design_menu.addAction(action)
        self.actions.append(action)
        return action

    def initGui(self):
        if self.actions or self.site_co_design_menu is not None or self.site_co_design_toolbar is not None:
            self.unload()
        try:
            self.site_co_design_menu = QMenu(self.tr('ODN Tools Pro'), self.iface.mainWindow())
            main_window = self.iface.mainWindow()
            try:
                first_right_menu = self.iface.firstRightStandardMenu()
                main_window.menuBar().insertMenu(first_right_menu.menuAction(), self.site_co_design_menu)
            except Exception:
                main_window.menuBar().addMenu(self.site_co_design_menu)
        except Exception:
            self.site_co_design_menu = None
        try:
            self.site_co_design_toolbar = self.iface.addToolBar('ODN Tools Pro')
            self.site_co_design_toolbar.setObjectName('ODNToolsProToolbar')
        except Exception:
            self.site_co_design_toolbar = None

        main_window = self.iface.mainWindow()
        self.add_action(os.path.join(self.plugin_dir, 'cableNaming.svg'), self.tr('光缆命名'), self.cable_naming, parent=main_window)
        self.add_action(os.path.join(self.plugin_dir, 'connectionPointNaming.svg'), self.tr('接入点命名'), self.connection_point_naming, parent=main_window)
        self.add_action(os.path.join(self.plugin_dir, 'cableSplit.svg'), self.tr('光缆分割'), self.cable_split, parent=main_window)
        self.add_action(os.path.join(self.plugin_dir, 'cableValidation.svg'), self.tr('Validation（校验）'), self.cable_validation, parent=main_window)

    def unload(self):
        for action in self.actions:
            try:
                action.deleteLater()
            except Exception:
                pass
        self.actions.clear()
        try:
            if self.site_co_design_toolbar is not None:
                self.iface.mainWindow().removeToolBar(self.site_co_design_toolbar)
        except Exception:
            pass
        self.site_co_design_toolbar = None
        try:
            if self.site_co_design_menu is not None:
                self.iface.mainWindow().menuBar().removeAction(self.site_co_design_menu.menuAction())
                self.site_co_design_menu.deleteLater()
        except Exception:
            pass
        self.site_co_design_menu = None
        if self.translator is not None:
            try:
                QCoreApplication.removeTranslator(self.translator)
            except Exception:
                pass
            self.translator = None

    def cable_naming(self):
        dialog_open(CableNamingDialog(self.iface))

    def connection_point_naming(self):
        dialog_open(ConnectionPointDialog(self.iface))

    def cable_split(self):
        dialog_open(CableSplitDialog(self.iface))

    def cable_validation(self):
        dialog_open(CableValidationDialog(self.iface))
