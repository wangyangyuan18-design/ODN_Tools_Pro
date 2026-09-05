# -*- coding: utf-8 -*-
"""
site_co_design_dialog.py

Dialogs for Site Co-Design plugin: CableNamingDialog and ConnectionPointDialog.
Both dialogs are lightweight, tightly laid-out, and restore simple settings from QgsProject.
"""
import os
from qgis.PyQt import QtWidgets, QtCore
from qgis.PyQt.QtGui import QFont
from qgis.core import QgsProject, QgsMapLayer, QgsWkbTypes
from .site_co_design_library import cable_naming_run, cable_split_run, populate_layer_controls, validation_run


class CableNamingDialog(QtWidgets.QDialog):
    def __init__(self, iface=None, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.setWindowTitle('光缆命名')
        self._build_ui()
        self._connect_signals()
        self._load_saved()
        # apply radio state
        try:
            self._update_field_mode()
        except Exception:
            pass

    def _build_ui(self):
        # Layout arranged per user spec (8 logical rows, compact and aligned)
        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)

        ctrl_w = 260

        # Row 1: 1.1 需要命名的线图层     1.2 下拉框
        grid.addWidget(QtWidgets.QLabel('需要命名的线图层'), 0, 0)
        self.lineLayerCombo = QtWidgets.QComboBox()
        self.lineLayerCombo.setMinimumWidth(ctrl_w)
        grid.addWidget(self.lineLayerCombo, 0, 1)

        # Row 2: 2.1 接入点图层    2.2 下拉框    2.3 汇聚点图层    2.4 下拉框
        grid.addWidget(QtWidgets.QLabel('接入点图层'), 1, 0)
        self.normLayerList = QtWidgets.QListWidget()
        self.normLayerList.setSelectionMode(QtWidgets.QAbstractItemView.MultiSelection)
        self.normLayerList.setMinimumWidth(ctrl_w)
        self.normLayerList.setMaximumHeight(120)
        grid.addWidget(self.normLayerList, 1, 1)

        grid.addWidget(QtWidgets.QLabel('汇聚点图层'), 1, 2)
        self.convLayerCombo = QtWidgets.QComboBox()
        self.convLayerCombo.setMinimumWidth(ctrl_w)
        grid.addWidget(self.convLayerCombo, 1, 3)

        # Row 3: fields for selected norm/conv layers
        grid.addWidget(QtWidgets.QLabel('接入点图层字段'), 2, 0)
        self.normFieldCombo = QtWidgets.QComboBox()
        self.normFieldCombo.setMinimumWidth(ctrl_w)
        grid.addWidget(self.normFieldCombo, 2, 1)

        grid.addWidget(QtWidgets.QLabel('汇聚点图层字段'), 2, 2)
        self.convFieldCombo = QtWidgets.QComboBox()
        self.convFieldCombo.setMinimumWidth(ctrl_w)
        grid.addWidget(self.convFieldCombo, 2, 3)

        # Row 4: 命名规则
        grid.addWidget(QtWidgets.QLabel('命名规则'), 3, 0)
        self.lineNameOrderCombo = QtWidgets.QComboBox()
        self.lineNameOrderCombo.addItems(['从汇聚点向末端方向命名', '从末端向汇聚点方向命名'])
        self.lineNameOrderCombo.setMinimumWidth(ctrl_w)
        grid.addWidget(self.lineNameOrderCombo, 3, 1)

        # Row 5: 汇聚点-接入点连接符
        grid.addWidget(QtWidgets.QLabel('汇聚点-接入点连接符'), 4, 0)
        self.sepEdit = QtWidgets.QLineEdit('-')
        self.sepEdit.setMinimumWidth(ctrl_w)
        grid.addWidget(self.sepEdit, 4, 1)

        # Row 6: 新增/修改 字段 options
        self.addRadio = QtWidgets.QRadioButton('新增字段')
        grid.addWidget(self.addRadio, 5, 0)
        self.addFieldEdit = QtWidgets.QLineEdit('Name')
        self.addFieldEdit.setMinimumWidth(ctrl_w)
        grid.addWidget(self.addFieldEdit, 5, 1)

        self.modRadio = QtWidgets.QRadioButton('修改字段')
        grid.addWidget(self.modRadio, 5, 2)
        self.modFieldCombo = QtWidgets.QComboBox()
        self.modFieldCombo.setMinimumWidth(ctrl_w)
        grid.addWidget(self.modFieldCombo, 5, 3)

        # Row 7: 示例预览
        grid.addWidget(QtWidgets.QLabel('示例预览'), 6, 0)
        self.previewValue = QtWidgets.QLabel('A - B - C')
        self.previewValue.setStyleSheet('color: blue; font-weight: bold')
        grid.addWidget(self.previewValue, 6, 1, 1, 3)

        # Row 8: 确定 / 取消 buttons (right aligned)
        self.okBtn = QtWidgets.QPushButton('确定')
        self.cancelBtn = QtWidgets.QPushButton('取消')
        btns = QtWidgets.QHBoxLayout()
        btns.addStretch()
        btns.addWidget(self.okBtn)
        btns.addWidget(self.cancelBtn)

        main = QtWidgets.QVBoxLayout()
        main.setContentsMargins(6, 6, 6, 6)
        main.addLayout(grid)
        main.addLayout(btns)
        self.setLayout(main)

        # keep behavior: populate layers after building UI
        self._populate_layers()

    def _populate_layers(self):
        proj = QgsProject.instance()
        self.lineLayerCombo.clear()
        self.normLayerList.clear()
        self.convLayerCombo.clear()

        for layer in proj.mapLayers().values():
            try:
                if layer.type() != QgsMapLayer.VectorLayer:
                    continue
                geom_type = QgsWkbTypes.geometryType(layer.wkbType())
            except Exception:
                continue

            if geom_type == QgsWkbTypes.LineGeometry:
                self.lineLayerCombo.addItem(layer.name())
            elif geom_type == QgsWkbTypes.PointGeometry:
                item = QtWidgets.QListWidgetItem(layer.name())
                item.setFlags(item.flags() | QtCore.Qt.ItemIsUserCheckable)
                item.setCheckState(QtCore.Qt.Unchecked)
                self.normLayerList.addItem(item)
                self.convLayerCombo.addItem(layer.name())

        # populate modFieldCombo for initial selection if possible
        self.modFieldCombo.clear()
        # populate conv field combo based on conv selection
        if self.convLayerCombo.currentText():
            self._on_conv_layer_changed(self.convLayerCombo.currentText())
        # populate norm field from first selected norm layer if any
        if self.normLayerList.count() > 0:
            item = self.normLayerList.item(0)
            item.setSelected(True)
            self._on_norm_list_changed()

    def _connect_signals(self):
        # norm layer list selection
        try:
            self.normLayerList.itemSelectionChanged.connect(self._on_norm_list_changed)
        except Exception:
            pass
        self.convLayerCombo.currentTextChanged.connect(self._on_conv_layer_changed)
        self.convFieldCombo.currentTextChanged.connect(self._update_preview)
        self.normFieldCombo.currentTextChanged.connect(self._update_preview)
        self.lineLayerCombo.currentTextChanged.connect(self._on_line_layer_changed)
        self.lineLayerCombo.currentTextChanged.connect(self._update_preview)
        self.sepEdit.textChanged.connect(self._update_preview)
        self.okBtn.clicked.connect(self._on_ok)
        self.cancelBtn.clicked.connect(self.reject)
        # radio toggles
        self.addRadio.toggled.connect(self._update_field_mode)
        self.modRadio.toggled.connect(self._update_field_mode)

    def _update_field_mode(self):
        add_mode = self.addRadio.isChecked()
        self.addFieldEdit.setEnabled(add_mode)
        self.modFieldCombo.setEnabled(not add_mode)

    def _on_conv_layer_changed(self, name):
        # populate conv field combo for cable naming dialog
        self.convFieldCombo.clear()
        if not name:
            return
        proj = QgsProject.instance()
        layers = proj.mapLayersByName(name)
        if not layers:
            return
        layer = layers[0]
        fields = [f.name() for f in layer.fields()]
        self.convFieldCombo.addItems(fields)

    def _on_norm_list_changed(self):
        # populate normFieldCombo based on currently highlighted/selected norm layer(s)
        self.normFieldCombo.clear()
        sel = []
        try:
            sel = self.normLayerList.selectedItems()
        except Exception:
            pass
        if not sel:
            return
        name = sel[0].text()
        proj = QgsProject.instance()
        layers = proj.mapLayersByName(name)
        if not layers:
            return
        layer = layers[0]
        fields = [f.name() for f in layer.fields()]
        self.normFieldCombo.addItems(fields)

    def _on_line_layer_changed(self, name):
        # populate modFieldCombo (fields for lines)
        self.modFieldCombo.clear()
        if not name:
            return
        proj = QgsProject.instance()
        layers = proj.mapLayersByName(name)
        if not layers:
            return
        layer = layers[0]
        fields = [f.name() for f in layer.fields()]
        self.modFieldCombo.addItems(fields)

    def _update_preview(self):
        try:
            sep = f" {self.sepEdit.text()} " if self.sepEdit.text() else ' - '
            # conv sample
            conv_sample = 'Conv'
            try:
                conv_layer_name = self.convLayerCombo.currentText()
                conv_field = self.convFieldCombo.currentText()
                if conv_layer_name and conv_field:
                    layers = QgsProject.instance().mapLayersByName(conv_layer_name)
                    if layers:
                        lyr = layers[0]
                        for f in lyr.getFeatures():
                            val = f.attribute(conv_field)
                            if val is not None and str(val).strip() != '':
                                conv_sample = str(val)
                                break
            except Exception:
                pass
            # norm sample from first selected norm layer
            norm_sample = 'Norm'
            try:
                sel = self.normLayerList.selectedItems()
                if sel:
                    name = sel[0].text()
                    field = self.normFieldCombo.currentText()
                    if name and field:
                        layers = QgsProject.instance().mapLayersByName(name)
                        if layers:
                            lyr = layers[0]
                            for f in lyr.getFeatures():
                                val = f.attribute(field)
                                if val is not None and str(val).strip() != '':
                                    norm_sample = str(val)
                                    break
            except Exception:
                pass
            self.previewValue.setText(f"{conv_sample}{sep}{norm_sample}")
        except Exception:
            try:
                self.previewValue.setText('A - B - C')
            except Exception:
                pass

    def _load_saved(self):
        proj = QgsProject.instance()
        ok, cfg = proj.readEntry('site_co_design', 'cableNaming')
        if ok and cfg:
            try:
                data = dict(cfg)
                if 'line' in data and data['line'] in [self.lineLayerCombo.itemText(i) for i in range(self.lineLayerCombo.count())]:
                    self.lineLayerCombo.setCurrentText(data['line'])
                # norm may be list or string
                if 'norm' in data:
                    try:
                        norms = data['norm']
                        if isinstance(norms, str):
                            norms = [norms]
                        # select items in normLayerList
                        for i in range(self.normLayerList.count()):
                            item = self.normLayerList.item(i)
                            if item.text() in norms:
                                item.setSelected(True)
                    except Exception:
                        pass
                if 'conv' in data and data['conv'] in [self.convLayerCombo.itemText(i) for i in range(self.convLayerCombo.count())]:
                    self.convLayerCombo.setCurrentText(data['conv'])
                if 'conv_field' in data:
                    try:
                        self._on_conv_layer_changed(self.convLayerCombo.currentText())
                        if data['conv_field'] in [self.convFieldCombo.itemText(i) for i in range(self.convFieldCombo.count())]:
                            self.convFieldCombo.setCurrentText(data['conv_field'])
                    except Exception:
                        pass
                if 'separator' in data:
                    self.sepEdit.setText(data.get('separator', '-'))
                if 'line_name_sort' in data:
                    self.lineNameOrderCombo.setCurrentText(data.get('line_name_sort', '从汇聚点向末端方向命名'))
                if 'add_mode' in data:
                    if data.get('add_mode', True):
                        self.addRadio.setChecked(True)
                    else:
                        self.modRadio.setChecked(True)
                if 'add_attr_name' in data:
                    self.addFieldEdit.setText(data.get('add_attr_name', ''))
                if 'modify_attr_name' in data and data['modify_attr_name'] in [self.modFieldCombo.itemText(i) for i in range(self.modFieldCombo.count())]:
                    self.modFieldCombo.setCurrentText(data.get('modify_attr_name', ''))
                # norm_field
                if 'norm_field' in data:
                    try:
                        self._on_norm_list_changed()
                        if data['norm_field'] in [self.normFieldCombo.itemText(i) for i in range(self.normFieldCombo.count())]:
                            self.normFieldCombo.setCurrentText(data['norm_field'])
                    except Exception:
                        pass
                self._update_preview()
            except Exception:
                pass

    def _on_ok(self):
        # collect selected norm layers
        norms = [item.text() for item in self.normLayerList.selectedItems()]
        params = {
            'line_layer_name': self.lineLayerCombo.currentText(),
            'conv_point_layer_name': self.convLayerCombo.currentText(),
            'conv_point_layer_field_name': self.convFieldCombo.currentText(),
            'norm_point_layer_name': norms,
            'norm_point_layer_field_name': self.normFieldCombo.currentText(),
            'line_name_sort_enum': 'fromConv' if self.lineNameOrderCombo.currentText().startswith('从汇聚点') else 'toConv',
            'separator': self.sepEdit.text(),
            'data_write_mode': 'addAttr' if self.addRadio.isChecked() else 'modifyAttr',
            'add_attr_name': self.addFieldEdit.text(),
            'modify_attr_name': self.modFieldCombo.currentText(),
        }
        # save
        proj = QgsProject.instance()
        proj.writeEntry('site_co_design', 'cableNaming', params)
        try:
            cable_naming_run(params, iface=self.iface)
        except Exception:
            pass
        self.accept()


class CableSplitDialog(QtWidgets.QDialog):
    def __init__(self, iface=None, parent=None):
        self.iface = iface
        parent_widget = parent
        if parent_widget is None and iface is not None:
            try:
                parent_widget = iface.mainWindow()
            except Exception:
                parent_widget = None
        super().__init__(parent_widget)
        self.setWindowTitle('光缆切割')
        self._build_ui()
        self._connect_signals()
        self._load_saved()

    def _build_ui(self):
        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        ctrl_w = 260

        grid.addWidget(QtWidgets.QLabel('线图层（Pre Connect Cable）'), 0, 0)
        self.lineLayerCombo = QtWidgets.QComboBox()
        self.lineLayerCombo.setMinimumWidth(ctrl_w)
        grid.addWidget(self.lineLayerCombo, 0, 1)

        grid.addWidget(QtWidgets.QLabel('点图层'), 1, 0)
        # Point layers are presented as a multi-select list with checkboxes
        self.fatLayerList = QtWidgets.QListWidget()
        self.fatLayerList.setMinimumWidth(ctrl_w)
        self.fatLayerList.setMaximumHeight(120)
        grid.addWidget(self.fatLayerList, 1, 1)


        self.tempRadio = QtWidgets.QRadioButton('临时图层加载')
        self.saveAsRadio = QtWidgets.QRadioButton('另存为文件')
        self.tempRadio.setChecked(True)
        grid.addWidget(self.tempRadio, 3, 0)
        grid.addWidget(self.saveAsRadio, 3, 1)

        grid.addWidget(QtWidgets.QLabel('输出路径'), 4, 0)
        self.outputPathEdit = QtWidgets.QLineEdit()
        self.outputPathEdit.setMinimumWidth(ctrl_w)
        self.outputPathEdit.setEnabled(False)
        grid.addWidget(self.outputPathEdit, 4, 1)
        self.outputPathBrowseBtn = QtWidgets.QPushButton('浏览...')
        self.outputPathBrowseBtn.setFixedWidth(80)
        self.outputPathBrowseBtn.setEnabled(False)
        grid.addWidget(self.outputPathBrowseBtn, 4, 2)

        self.okBtn = QtWidgets.QPushButton('确定')
        self.cancelBtn = QtWidgets.QPushButton('取消')
        btns = QtWidgets.QHBoxLayout()
        btns.addStretch()
        btns.addWidget(self.okBtn)
        btns.addWidget(self.cancelBtn)

        main = QtWidgets.QVBoxLayout()
        main.setContentsMargins(6, 6, 6, 6)
        main.addLayout(grid)
        main.addLayout(btns)
        self.setLayout(main)

        # populate with only line (single) and point (multi) geometry layers
        try:
            populate_layer_controls(self.lineLayerCombo, self.fatLayerList)
        except Exception:
            # fallback to legacy population
            self._populate_layers()

    def _populate_layers(self):
        # Use populate_layer_controls to list only Line (single-select) and Point (multi-select) layers
        try:
            populate_layer_controls(self.lineLayerCombo, self.fatLayerList)
        except Exception:
            # fallback: populate combos broadly
            proj = QgsProject.instance()
            layers = proj.mapLayers().values()
            names = sorted([l.name() for l in layers])
            self.lineLayerCombo.clear()
            self.lineLayerCombo.addItems(names)
            # populate fat list as checkable items
            self.fatLayerList.clear()
            from qgis.PyQt import QtCore
            for n in names:
                item = QtWidgets.QListWidgetItem(n)
                item.setFlags(item.flags() | QtCore.Qt.ItemIsUserCheckable)
                item.setCheckState(QtCore.Qt.Unchecked)
                self.fatLayerList.addItem(item)

    def _connect_signals(self):
        self.okBtn.clicked.connect(self._on_ok)
        self.cancelBtn.clicked.connect(self.reject)
        self.outputPathBrowseBtn.clicked.connect(self._browse_output_path)
        self.tempRadio.toggled.connect(self._update_output_mode)
        self.saveAsRadio.toggled.connect(self._update_output_mode)

    def _load_saved(self):
        proj = QgsProject.instance()
        ok, cfg = proj.readEntry('site_co_design', 'cableSplit')
        if ok and cfg:
            try:
                data = dict(cfg)
                if 'line' in data and data['line'] in [self.lineLayerCombo.itemText(i) for i in range(self.lineLayerCombo.count())]:
                    self.lineLayerCombo.setCurrentText(data['line'])
                # restore point layer selections (support list or comma/semicolon-separated string)
                if 'point_layers' in data:
                    val = data['point_layers']
                    names = []
                    if isinstance(val, list):
                        names = val
                    elif isinstance(val, str):
                        if ',' in val or ';' in val:
                            import re
                            names = [s.strip() for s in re.split('[,;]', val) if s.strip()]
                        elif val.strip() == '':
                            names = []
                        else:
                            names = [val.strip()]
                    # apply checks to the fatLayerList
                    try:
                        for i in range(self.fatLayerList.count()):
                            item = self.fatLayerList.item(i)
                            if item.text() in names:
                                item.setCheckState(QtCore.Qt.Checked)
                            else:
                                item.setCheckState(QtCore.Qt.Unchecked)
                    except Exception:
                        pass
                if 'output_mode' in data:
                    if data.get('output_mode') == 'save':
                        self.saveAsRadio.setChecked(True)
                    else:
                        self.tempRadio.setChecked(True)
                if 'output_path' in data:
                    self.outputPathEdit.setText(data.get('output_path', ''))
                    if not data.get('output_mode') and data.get('output_path'):
                        self.saveAsRadio.setChecked(True)
            except Exception:
                pass

    def _browse_output_path(self):
        default_path = self.outputPathEdit.text().strip() or QgsProject.instance().homePath() or ''
        file_path, _ = QtWidgets.QFileDialog.getSaveFileName(self, '选择输出路径', default_path, 'GeoPackage (*.gpkg);;ESRI Shapefile (*.shp);;GeoJSON (*.geojson)')
        if file_path:
            self.outputPathEdit.setText(file_path)

    def _update_output_mode(self):
        save_as = self.saveAsRadio.isChecked()
        self.outputPathEdit.setEnabled(save_as)
        self.outputPathBrowseBtn.setEnabled(save_as)

    def _on_ok(self):
        output_path = self.outputPathEdit.text().strip() if self.saveAsRadio.isChecked() else ''
        # collect selected FAT layers from the checkable list
        selected_fats = []
        try:
            for i in range(self.fatLayerList.count()):
                item = self.fatLayerList.item(i)
                if item.checkState() == QtCore.Qt.Checked:
                    selected_fats.append(item.text())
        except Exception:
            selected_fats = []

        if not selected_fats:
            QtWidgets.QMessageBox.critical(self, 'Site Co-Design', '请至少选择一个 FAT（点）图层')
            return

        params = {
            'line_layer_name': self.lineLayerCombo.currentText(),
            'point_layer_names': selected_fats,  # pass list to cable_split_run as point layers
            'output_mode': 'save' if self.saveAsRadio.isChecked() else 'temp',
            'output_path': output_path,
        }
        if self.saveAsRadio.isChecked() and not output_path:
            QtWidgets.QMessageBox.critical(self, 'Site Co-Design', '请选择输出路径或改为临时图层加载')
            return
        proj = QgsProject.instance()
        # Save fat layers as comma-separated string for project settings compatibility
        save_cfg = dict(params)
        try:
            save_cfg['point_layers'] = ','.join(selected_fats)
        except Exception:
            save_cfg['point_layers'] = selected_fats
        proj.writeEntry('site_co_design', 'cableSplit', save_cfg)
        try:
            cable_split_run(params, iface=self.iface)
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, 'Site Co-Design', f'Cable Split 运行出错: {str(e)}')
        self.accept()


class ConnectionPointDialog(QtWidgets.QDialog):
    def __init__(self, iface=None, parent=None):
        # Accept either iface (QgisInterface) or a QWidget parent. If iface is provided
        # and parent is None, use iface.mainWindow() as the dialog parent.
        self.iface = iface
        parent_widget = parent
        if parent_widget is None and iface is not None:
            try:
                parent_widget = iface.mainWindow()
            except Exception:
                parent_widget = None
        super().__init__(parent_widget)
        self.setWindowTitle('接入点命名')
        self._build_ui()
        self._connect_signals()
        self._load_saved()
        # apply radio state
        try:
            self._update_field_mode()
        except Exception:
            pass

    def _build_ui(self):
        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)

        # unified control width
        ctrl_w = 200

        # 1. 接入点图层 (需要命名的图层-接入点图层)
        grid.addWidget(QtWidgets.QLabel('需要命名的图层-接入点图层'), 0, 0)
        self.normLayerCombo = QtWidgets.QComboBox()
        self.normLayerCombo.setMinimumWidth(ctrl_w)
        grid.addWidget(self.normLayerCombo, 0, 1)

        # 2. 汇聚点图层 和 字段 在同一行
        grid.addWidget(QtWidgets.QLabel('汇聚点图层'), 1, 0)
        self.convLayerCombo = QtWidgets.QComboBox()
        self.convLayerCombo.setMinimumWidth(ctrl_w)
        grid.addWidget(self.convLayerCombo, 1, 1)

        grid.addWidget(QtWidgets.QLabel('字段名'), 1, 2)
        self.convFieldCombo = QtWidgets.QComboBox()
        self.convFieldCombo.setMinimumWidth(ctrl_w)
        grid.addWidget(self.convFieldCombo, 1, 3)

        # 3. 线图层
        grid.addWidget(QtWidgets.QLabel('连接汇聚点与接入点图层'), 2, 0)
        self.lineLayerCombo = QtWidgets.QComboBox()
        self.lineLayerCombo.setMinimumWidth(ctrl_w)
        grid.addWidget(self.lineLayerCombo, 2, 1)

        # 4. 命名规则
        grid.addWidget(QtWidgets.QLabel('命名规则'), 3, 0)
        self.nameOrderCombo = QtWidgets.QComboBox()
        self.nameOrderCombo.addItems(['从汇聚点到末端', '从末端到汇聚点'])
        self.nameOrderCombo.setMinimumWidth(ctrl_w)
        grid.addWidget(self.nameOrderCombo, 3, 1)

        # 5. 汇聚点线段排序规则
        grid.addWidget(QtWidgets.QLabel('汇聚点线段排序规则'), 3, 2)
        self.sortDirCombo = QtWidgets.QComboBox()
        self.sortDirCombo.addItems(['顺时针', '逆时针'])
        self.sortDirCombo.setMinimumWidth(ctrl_w)
        grid.addWidget(self.sortDirCombo, 3, 3)

        # 6.1 & 6.2 变量前缀在同一行
        grid.addWidget(QtWidgets.QLabel('接入点名称变量L前缀'), 4, 0)
        self.varLEdit = QtWidgets.QLineEdit('L')
        self.varLEdit.setMinimumWidth(ctrl_w)
        grid.addWidget(self.varLEdit, 4, 1)

        grid.addWidget(QtWidgets.QLabel('接入点名称变量S前缀'), 4, 2)
        self.varSEdit = QtWidgets.QLineEdit('S')
        self.varSEdit.setMinimumWidth(ctrl_w)
        grid.addWidget(self.varSEdit, 4, 3)

        # 6.3 & 6.4 后缀在同一行
        grid.addWidget(QtWidgets.QLabel('接入点名称变量L后缀'), 5, 0)
        self.varLSufCombo = QtWidgets.QComboBox()
        self.varLSufCombo.addItems(['num', 'letter'])
        self.varLSufCombo.setMinimumWidth(ctrl_w)
        grid.addWidget(self.varLSufCombo, 5, 1)

        grid.addWidget(QtWidgets.QLabel('接入点名称变量S后缀'), 5, 2)
        self.varSSufCombo = QtWidgets.QComboBox()
        self.varSSufCombo.addItems(['num', 'letter'])
        self.varSSufCombo.setMinimumWidth(ctrl_w)
        grid.addWidget(self.varSSufCombo, 5, 3)

        # 6.5 汇聚点前缀（工程/OLT名）与预览
        grid.addWidget(QtWidgets.QLabel('汇聚点前缀（工程/OLT名）'), 6, 0)
        self.prefixEdit = QtWidgets.QLineEdit('')
        self.prefixEdit.setMinimumWidth(ctrl_w)
        grid.addWidget(self.prefixEdit, 6, 1)

        # placeholder label for alignment
        grid.addWidget(QtWidgets.QLabel(''), 6, 2)
        self.previewLabel = QtWidgets.QLabel('示例预览')
        self.previewLabel.setAlignment(QtCore.Qt.AlignLeft)
        grid.addWidget(self.previewLabel, 6, 3)

        # preview value directly under varSSufCombo (blue)
        self.previewValue = QtWidgets.QLabel('HB01L1S1')
        self.previewValue.setStyleSheet('color: blue; font-weight: bold')
        grid.addWidget(self.previewValue, 7, 3)

        # 7.1 新增字段 / 新增字段名 一行
        self.addRadio = QtWidgets.QRadioButton('新增字段')
        grid.addWidget(self.addRadio, 8, 0)
        self.addFieldEdit = QtWidgets.QLineEdit('')
        self.addFieldEdit.setMinimumWidth(ctrl_w)
        grid.addWidget(self.addFieldEdit, 8, 1)

        # 7.3 修改字段 / 修改字段名 在同一行
        self.modRadio = QtWidgets.QRadioButton('修改字段')
        grid.addWidget(self.modRadio, 8, 2)
        self.modFieldCombo = QtWidgets.QComboBox()
        self.modFieldCombo.setMinimumWidth(ctrl_w)
        grid.addWidget(self.modFieldCombo, 8, 3)

        # ensure radio exclusivity
        rg = QtWidgets.QButtonGroup(self)
        rg.addButton(self.addRadio)
        rg.addButton(self.modRadio)
        self.addRadio.setChecked(True)

        # buttons - left aligned
        btns = QtWidgets.QHBoxLayout()
        btns.setSpacing(8)
        self.okBtn = QtWidgets.QPushButton('确定')
        self.cancelBtn = QtWidgets.QPushButton('取消')
        btns.addWidget(self.okBtn)
        btns.addWidget(self.cancelBtn)

        main = QtWidgets.QVBoxLayout()
        main.setContentsMargins(6, 6, 6, 6)
        main.addLayout(grid)
        main.addLayout(btns)
        self.setLayout(main)

        self._populate_layers()

    def _populate_layers(self):
        proj = QgsProject.instance()
        self.normLayerCombo.clear()
        self.convLayerCombo.clear()
        self.lineLayerCombo.clear()

        for layer in proj.mapLayers().values():
            try:
                if layer.type() != QgsMapLayer.VectorLayer:
                    continue
                geom_type = QgsWkbTypes.geometryType(layer.wkbType())
            except Exception:
                continue

            if geom_type == QgsWkbTypes.LineGeometry:
                self.lineLayerCombo.addItem(layer.name())
            elif geom_type == QgsWkbTypes.PointGeometry:
                self.normLayerCombo.addItem(layer.name())
                self.convLayerCombo.addItem(layer.name())

        self.modFieldCombo.clear()
        cur_norm = self.normLayerCombo.currentText()
        if cur_norm:
            self._on_norm_layer_changed(cur_norm)

    def _connect_signals(self):
        self.convLayerCombo.currentTextChanged.connect(self._on_conv_layer_changed)
        self.normLayerCombo.currentTextChanged.connect(self._on_norm_layer_changed)
        self.varLEdit.textChanged.connect(self._update_preview)
        self.varSEdit.textChanged.connect(self._update_preview)
        self.varLSufCombo.currentTextChanged.connect(self._update_preview)
        self.varSSufCombo.currentTextChanged.connect(self._update_preview)
        self.prefixEdit.textChanged.connect(self._update_preview)
        self.convFieldCombo.currentTextChanged.connect(self._update_preview)
        self.okBtn.clicked.connect(self._on_ok)
        self.cancelBtn.clicked.connect(self._on_cancel)
        # radio toggles
        self.addRadio.toggled.connect(self._update_field_mode)
        self.modRadio.toggled.connect(self._update_field_mode)
        # when switching to modify mode, populate modFieldCombo
        self.modRadio.toggled.connect(self._on_mod_radio_toggled)

    def _update_field_mode(self):
        add_mode = self.addRadio.isChecked()
        self.addFieldEdit.setEnabled(add_mode)
        self.modFieldCombo.setEnabled(not add_mode)

    def _on_mod_radio_toggled(self, checked):
        if checked:
            try:
                self._populate_mod_fields()
            except Exception:
                pass

    def _populate_mod_fields(self):
        # populate modFieldCombo with fields from the selected norm layer
        self.modFieldCombo.clear()
        name = self.normLayerCombo.currentText()
        if not name:
            return
        proj = QgsProject.instance()
        layers = proj.mapLayersByName(name)
        if not layers:
            return
        layer = layers[0]
        fields = [f.name() for f in layer.fields()]
        self.modFieldCombo.addItems(fields)

    def _on_conv_layer_changed(self, name):
        self.convFieldCombo.clear()
        if not name:
            return
        proj = QgsProject.instance()
        layers = proj.mapLayersByName(name)
        if not layers:
            return
        layer = layers[0]
        fields = [f.name() for f in layer.fields()]
        self.convFieldCombo.addItems(fields)

    def _on_norm_list_changed(self):
        # populate normFieldCombo based on currently highlighted/selected norm layer(s)
        self.normFieldCombo.clear()
        sel = []
        try:
            sel = self.normLayerList.selectedItems()
        except Exception:
            pass
        if not sel:
            return
        name = sel[0].text()
        proj = QgsProject.instance()
        layers = proj.mapLayersByName(name)
        if not layers:
            return
        layer = layers[0]
        fields = [f.name() for f in layer.fields()]
        self.normFieldCombo.addItems(fields)

    def _on_line_layer_changed(self, name):
        # populate modFieldCombo (fields for lines)
        self.modFieldCombo.clear()
        if not name:
            return
        proj = QgsProject.instance()
        layers = proj.mapLayersByName(name)
        if not layers:
            return
        layer = layers[0]
        fields = [f.name() for f in layer.fields()]
        self.modFieldCombo.addItems(fields)

    def _on_norm_layer_changed(self, name):
        # wrapper to support legacy single-name handlers; delegate to list-based handler
        try:
            self._on_norm_list_changed()
        except Exception:
            pass

    def _update_preview(self):
        try:
            prefix = self.prefixEdit.text() if hasattr(self, 'prefixEdit') else ''
            conv_field = self.convFieldCombo.currentText() or ''
            field_sample = 'HB01'
            try:
                if conv_field and self.convLayerCombo.currentText():
                    layers = QgsProject.instance().mapLayersByName(self.convLayerCombo.currentText())
                    if layers:
                        lyr = layers[0]
                        for f in lyr.getFeatures():
                            val = f.attribute(conv_field)
                            if val is not None and str(val).strip() != '':
                                field_sample = str(val)
                                break
            except Exception:
                pass
            l = self.varLEdit.text() or 'L'
            s = self.varSEdit.text() or 'S'
            lsuf = self.varLSufCombo.currentText()
            ssuf = self.varSSufCombo.currentText()
            if lsuf == 'num':
                Lpart = f"{l}1"
            else:
                Lpart = f"{l}A"
            if ssuf == 'num':
                Spart = f"{s}1"
            else:
                Spart = f"{s}A"
            prefix_part = prefix or ''
            self.previewValue.setText(f"{prefix_part}{field_sample}{Lpart}{Spart}")
        except Exception:
            pass

    def _load_saved(self):
        # restore last choices from project variables
        proj = QgsProject.instance()
        ok, cfg = proj.readEntry('site_co_design', 'connectionPointNaming')
        if ok and cfg:
            try:
                data = dict(cfg)
                if 'norm' in data and data['norm'] in [self.normLayerCombo.itemText(i) for i in range(self.normLayerCombo.count())]:
                    self.normLayerCombo.setCurrentText(data['norm'])
                if 'conv' in data and data['conv'] in [self.convLayerCombo.itemText(i) for i in range(self.convLayerCombo.count())]:
                    self.convLayerCombo.setCurrentText(data['conv'])
                if 'line' in data and data['line'] in [self.lineLayerCombo.itemText(i) for i in range(self.lineLayerCombo.count())]:
                    self.lineLayerCombo.setCurrentText(data['line'])
                if 'conv_field' in data and data['conv_field'] in [self.convFieldCombo.itemText(i) for i in range(self.convFieldCombo.count())]:
                    self.convFieldCombo.setCurrentText(data['conv_field'])
                if 'line_name_sort' in data:
                    self.nameOrderCombo.setCurrentText(data.get('line_name_sort', '从汇聚点到末端'))
                if 'sort_dir' in data:
                    self.sortDirCombo.setCurrentText(data.get('sort_dir', '顺时针'))
                if 'varL' in data:
                    self.varLEdit.setText(data.get('varL', 'L'))
                if 'varS' in data:
                    self.varSEdit.setText(data.get('varS', 'S'))
                if 'variableLSuf' in data:
                    self.varLSufCombo.setCurrentText(data.get('variableLSuf', 'num'))
                if 'variableSSuf' in data:
                    self.varSSufCombo.setCurrentText(data.get('variableSSuf', 'num'))
                if 'prefix' in data:
                    self.prefixEdit.setText(data.get('prefix', ''))
                if 'add_mode' in data:
                    if data.get('add_mode', True):
                        self.addRadio.setChecked(True)
                    else:
                        self.modRadio.setChecked(True)
                if 'add_attr_name' in data:
                    self.addFieldEdit.setText(data.get('add_attr_name', ''))
                if 'modify_attr_name' in data and data['modify_attr_name'] in [self.modFieldCombo.itemText(i) for i in range(self.modFieldCombo.count())]:
                    self.modFieldCombo.setCurrentText(data.get('modify_attr_name', ''))
                self._on_conv_layer_changed(self.convLayerCombo.currentText())
                self._on_norm_layer_changed(self.normLayerCombo.currentText())
                self._update_preview()
            except Exception:
                pass

    def _on_ok(self):
        params = {
            'line_layer_name': self.lineLayerCombo.currentText(),
            'conv_point_layer_name': self.convLayerCombo.currentText(),
            'conv_point_layer_field_name': self.convFieldCombo.currentText(),
            'norm_point_layer_name': self.normLayerCombo.currentText(),
            'prefix': self.prefixEdit.text(),
            'point_name_sort_enum': 'fromConv' if self.nameOrderCombo.currentText().startswith('从汇聚') else 'toConv',
            'point_name_directions_enum': 'clockwise' if self.sortDirCombo.currentText() == '顺时针' else 'counter',
            'variableL': self.varLEdit.text(),
            'variableS': self.varSEdit.text(),
            'variableLSuf': self.varLSufCombo.currentText(),
            'variableSSuf': self.varSSufCombo.currentText(),
            'data_write_mode': 'addAttr' if self.addRadio.isChecked() else 'modifyAttr',
            'add_attr_name': self.addFieldEdit.text(),
            'modify_attr_name': self.modFieldCombo.currentText(),
        }
        proj = QgsProject.instance()
        proj.writeEntry('site_co_design', 'connectionPointNaming', params)
        try:
            from .site_co_design_library import connection_point_naming_run
            connection_point_naming_run(params, iface=self.iface)
        except Exception:
            pass
        self.accept()

    def _on_cancel(self):
        self.reject()

    def values(self):
        return {
            'norm_layer': self.normLayerCombo.currentText(),
            'conv_layer': self.convLayerCombo.currentText(),
            'conv_field': self.convFieldCombo.currentText(),
        }


class CableValidationDialog(QtWidgets.QDialog):
    """
    New unified Validation dialog (compact layout).

    Layout (left aligned):
      Line Layer:   [ combo ]
      Point Layer:  [ combo ]
      Validation:   [ combo ]
      [ Run ]

    Below Run there are two checkboxes:
      - List abnormal point names
      - Select abnormal points

    The dialog gathers UI parameters and calls validation_run(params, iface=self.iface).
    """
    VALIDATION_ITEMS = ['Point not on line', 'Point not on cable vertex']

    def __init__(self, iface=None, parent=None):
        self.iface = iface
        parent_widget = parent
        if parent_widget is None and iface is not None:
            try:
                parent_widget = iface.mainWindow()
            except Exception:
                parent_widget = None
        super().__init__(parent_widget)
        # title changed to generic Validation (校验)
        self.setWindowTitle('校验')
        self._build_ui()
        self._connect_signals()
        self._populate_layers()
        self._load_saved()

    def _build_ui(self):
        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        ctrl_w = 300

        # Line Layer
        grid.addWidget(QtWidgets.QLabel('Line Layer：'), 0, 0)
        self.lineLayerCombo = QtWidgets.QComboBox()
        self.lineLayerCombo.setMinimumWidth(ctrl_w)
        grid.addWidget(self.lineLayerCombo, 0, 1)

        # Point Layer
        grid.addWidget(QtWidgets.QLabel('Point Layer：'), 1, 0)
        self.pointLayerCombo = QtWidgets.QComboBox()
        self.pointLayerCombo.setMinimumWidth(ctrl_w)
        grid.addWidget(self.pointLayerCombo, 1, 1)

        # Validation
        grid.addWidget(QtWidgets.QLabel('Validation：'), 2, 0)
        self.validationCombo = QtWidgets.QComboBox()
        self.validationCombo.setMinimumWidth(ctrl_w)
        self.validationCombo.addItems(self.VALIDATION_ITEMS)
        grid.addWidget(self.validationCombo, 2, 1)

        # Run button (left aligned)
        self.runBtn = QtWidgets.QPushButton('Run')
        btns = QtWidgets.QHBoxLayout()
        btns.addWidget(self.runBtn)
        btns.addStretch()

        # Options (two checkboxes)
        self.listNamesChk = QtWidgets.QCheckBox('List abnormal point names')
        self.selectPointsChk = QtWidgets.QCheckBox('Select abnormal points')

        # overall layout
        main = QtWidgets.QVBoxLayout()
        main.setContentsMargins(6, 6, 6, 6)
        main.addLayout(grid)
        main.addLayout(btns)
        main.addWidget(self.listNamesChk)
        main.addWidget(self.selectPointsChk)
        # compact spacing
        main.addStretch()
        self.setLayout(main)

    def _connect_signals(self):
        self.runBtn.clicked.connect(self._on_run)
        self.lineLayerCombo.currentTextChanged.connect(self._on_line_changed)

    def _populate_layers(self):
        proj = QgsProject.instance()
        self.lineLayerCombo.clear()
        self.pointLayerCombo.clear()
        for layer in proj.mapLayers().values():
            try:
                if layer.type() != QgsMapLayer.VectorLayer:
                    continue
                geom_type = QgsWkbTypes.geometryType(layer.wkbType())
            except Exception:
                continue
            if geom_type == QgsWkbTypes.LineGeometry:
                self.lineLayerCombo.addItem(layer.name())
            elif geom_type == QgsWkbTypes.PointGeometry:
                self.pointLayerCombo.addItem(layer.name())

    def _on_line_changed(self, name):
        # if needed in future: update dependent controls
        pass

    def _load_saved(self):
        proj = QgsProject.instance()
        ok, cfg = proj.readEntry('site_co_design', 'validation')
        if ok and cfg:
            try:
                data = dict(cfg)
                if 'line' in data and data['line'] in [self.lineLayerCombo.itemText(i) for i in range(self.lineLayerCombo.count())]:
                    self.lineLayerCombo.setCurrentText(data['line'])
                if 'point' in data and data['point'] in [self.pointLayerCombo.itemText(i) for i in range(self.pointLayerCombo.count())]:
                    self.pointLayerCombo.setCurrentText(data['point'])
                if 'item' in data and data['item'] in [self.validationCombo.itemText(i) for i in range(self.validationCombo.count())]:
                    self.validationCombo.setCurrentText(data['item'])
                if 'list_names' in data:
                    try:
                        self.listNamesChk.setChecked(bool(data.get('list_names')))
                    except Exception:
                        pass
                if 'select_results' in data:
                    try:
                        self.selectPointsChk.setChecked(bool(data.get('select_results')))
                    except Exception:
                        pass
            except Exception:
                pass

    def _on_run(self):
        line = self.lineLayerCombo.currentText()
        point = self.pointLayerCombo.currentText()
        item = self.validationCombo.currentText()
        if not line:
            QtWidgets.QMessageBox.critical(self, 'Site Co-Design', 'Please select a Line Layer')
            return
        if not point:
            QtWidgets.QMessageBox.critical(self, 'Site Co-Design', 'Please select a Point Layer')
            return
        params = {
            'line_layer_name': line,
            'point_layer_name': point,
            'validation_item': item,
            'list_names': bool(self.listNamesChk.isChecked()),
            'select_results': bool(self.selectPointsChk.isChecked()),
            # future: tolerance can be added
        }
        # save settings
        try:
            proj = QgsProject.instance()
            save = dict(params)
            save['line'] = save.pop('line_layer_name')
            save['point'] = save.pop('point_layer_name')
            save['item'] = save.get('validation_item')
            proj.writeEntry('site_co_design', 'validation', save)
        except Exception:
            pass

        try:
            # call unified validation runner in library
            from .site_co_design_library import validation_run
            validation_run(params, iface=self.iface)
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, 'Site Co-Design', f'Validation 运行出错: {str(e)}')
        # keep dialog open so user can inspect/modify options



# module exports
__all__ = ['CableNamingDialog', 'ConnectionPointDialog', 'CableValidationDialog']
