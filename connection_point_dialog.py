# -*- coding: utf-8 -*-
"""Connection Point Naming dialog for ODN Tools Pro."""

from qgis.PyQt import QtCore, QtWidgets
from qgis.core import QgsMapLayer, QgsProject, QgsWkbTypes


class ConnectionPointDialog(QtWidgets.QDialog):
    """Configure FDT and FAT/CL/BB naming, with optional cable naming."""

    def __init__(self, iface=None, parent=None):
        self.iface = iface
        if parent is None and iface is not None:
            try:
                parent = iface.mainWindow()
            except Exception:
                parent = None
        super().__init__(parent)
        self.setWindowTitle('接入点命名')
        self.setMinimumWidth(760)
        self._refreshing_fields = False
        self._build_ui()
        self._connect_signals()
        self._populate_layers()
        self._load_saved()
        self._update_field_mode()
        self._update_line_mode()
        self._update_previews()

    def _section(self, title):
        label = QtWidgets.QLabel(title)
        font = label.font()
        font.setBold(True)
        label.setFont(font)
        return label

    def _combo(self, width=230):
        combo = QtWidgets.QComboBox()
        combo.setMinimumWidth(width)
        return combo

    @staticmethod
    def _style_preview(label):
        label.setStyleSheet('color: blue; font-weight: bold')

    def _build_ui(self):
        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)
        row = 0

        grid.addWidget(self._section('① 网络图层'), row, 0, 1, 4)
        row += 1
        grid.addWidget(QtWidgets.QLabel('汇聚点图层'), row, 0)
        self.fdtLayerCombo = self._combo()
        grid.addWidget(self.fdtLayerCombo, row, 1)
        grid.addWidget(QtWidgets.QLabel('字段名'), row, 2)
        self.fdtFieldCombo = self._combo()
        grid.addWidget(self.fdtFieldCombo, row, 3)
        row += 1
        grid.addWidget(QtWidgets.QLabel('连接汇聚点与接入点图层'), row, 0)
        self.lineLayerCombo = self._combo(300)
        grid.addWidget(self.lineLayerCombo, row, 1, 1, 3)
        row += 1

        grid.addWidget(self._section('② 一级链路'), row, 0, 1, 4)
        row += 1
        grid.addWidget(QtWidgets.QLabel('命名规则'), row, 0)
        self.nameOrderCombo = self._combo()
        self.nameOrderCombo.addItems(['从汇聚点到末端', '从末端到汇聚点'])
        grid.addWidget(self.nameOrderCombo, row, 1)
        grid.addWidget(QtWidgets.QLabel('排序规则'), row, 2)
        self.sortDirCombo = self._combo()
        self.sortDirCombo.addItems(['顺时针', '逆时针'])
        grid.addWidget(self.sortDirCombo, row, 3)
        row += 1
        grid.addWidget(QtWidgets.QLabel('L前缀'), row, 0)
        self.lPrefixEdit = QtWidgets.QLineEdit('_L')
        grid.addWidget(self.lPrefixEdit, row, 1)
        grid.addWidget(QtWidgets.QLabel('L后缀'), row, 2)
        self.lSuffixCombo = self._combo()
        self.lSuffixCombo.addItems(['num', 'letter', ''])
        grid.addWidget(self.lSuffixCombo, row, 3)
        row += 1
        grid.addWidget(QtWidgets.QLabel('汇聚点前缀（工程/OLT名）'), row, 0)
        self.fdtPrefixEdit = QtWidgets.QLineEdit('')
        grid.addWidget(self.fdtPrefixEdit, row, 1, 1, 3)
        row += 1

        grid.addWidget(self._section('③ 节点图层'), row, 0, 1, 4)
        row += 1
        self.fatEnable = QtWidgets.QCheckBox('接入节点（FAT）')
        self.fatLayerCombo = self._combo(260)
        grid.addWidget(self.fatEnable, row, 0)
        grid.addWidget(self.fatLayerCombo, row, 1, 1, 3)
        row += 1
        self.clEnable = QtWidgets.QCheckBox('串接节点（CL）')
        self.clLayerCombo = self._combo(260)
        grid.addWidget(self.clEnable, row, 0)
        grid.addWidget(self.clLayerCombo, row, 1, 1, 3)
        row += 1
        self.bbEnable = QtWidgets.QCheckBox('分叉节点（BB）')
        self.bbLayerCombo = self._combo(260)
        grid.addWidget(self.bbEnable, row, 0)
        grid.addWidget(self.bbLayerCombo, row, 1, 1, 3)
        row += 1

        grid.addWidget(self._section('④ FAT 命名规则'), row, 0, 1, 4)
        row += 1
        grid.addWidget(QtWidgets.QLabel('S前缀'), row, 0)
        self.fatPrefixEdit = QtWidgets.QLineEdit('_S')
        grid.addWidget(self.fatPrefixEdit, row, 1)
        grid.addWidget(QtWidgets.QLabel('S后缀'), row, 2)
        self.fatSuffixCombo = self._combo()
        self.fatSuffixCombo.addItems(['num', 'letter', ''])
        grid.addWidget(self.fatSuffixCombo, row, 3)
        row += 1
        grid.addWidget(QtWidgets.QLabel('Preview'), row, 0)
        self.fatPreview = QtWidgets.QLabel('HB01_L1_S1')
        self._style_preview(self.fatPreview)
        grid.addWidget(self.fatPreview, row, 1, 1, 3)
        row += 1

        grid.addWidget(self._section('⑤ CL 命名规则'), row, 0, 1, 4)
        row += 1
        grid.addWidget(QtWidgets.QLabel('CL前缀'), row, 0)
        self.clPrefixEdit = QtWidgets.QLineEdit('_CL')
        grid.addWidget(self.clPrefixEdit, row, 1)
        grid.addWidget(QtWidgets.QLabel('CL后缀'), row, 2)
        self.clSuffixCombo = self._combo()
        self.clSuffixCombo.addItems(['num', 'letter', ''])
        grid.addWidget(self.clSuffixCombo, row, 3)
        row += 1
        grid.addWidget(QtWidgets.QLabel('Preview'), row, 0)
        self.clPreview = QtWidgets.QLabel('HB01_L1_CL1')
        self._style_preview(self.clPreview)
        grid.addWidget(self.clPreview, row, 1, 1, 3)
        row += 1

        grid.addWidget(self._section('⑥ BB 命名规则'), row, 0, 1, 4)
        row += 1
        grid.addWidget(QtWidgets.QLabel('BB前缀'), row, 0)
        self.bbPrefixEdit = QtWidgets.QLineEdit('_BB')
        grid.addWidget(self.bbPrefixEdit, row, 1)
        self.bbAutoCheck = QtWidgets.QCheckBox('Auto 5050/3070')
        self.bbAutoCheck.setChecked(True)
        grid.addWidget(self.bbAutoCheck, row, 2, 1, 2)
        row += 1
        grid.addWidget(QtWidgets.QLabel('Preview'), row, 0)
        self.bbPreview = QtWidgets.QLabel('HB01_L1_BB5050')
        self._style_preview(self.bbPreview)
        grid.addWidget(self.bbPreview, row, 1, 1, 3)
        row += 1

        grid.addWidget(self._section('⑦ 接入节点输出字段'), row, 0, 1, 4)
        row += 1
        self.addRadio = QtWidgets.QRadioButton('新增字段')
        self.modRadio = QtWidgets.QRadioButton('修改字段')
        self.addFieldEdit = QtWidgets.QLineEdit('Name')
        self.modFieldCombo = self._combo(260)
        self.fieldModeGroup = QtWidgets.QButtonGroup(self)
        self.fieldModeGroup.setExclusive(True)
        self.fieldModeGroup.addButton(self.addRadio)
        self.fieldModeGroup.addButton(self.modRadio)
        self.addRadio.setChecked(True)
        grid.addWidget(self.addRadio, row, 0)
        grid.addWidget(self.addFieldEdit, row, 1)
        grid.addWidget(self.modRadio, row, 2)
        grid.addWidget(self.modFieldCombo, row, 3)
        row += 1

        self.lineNameEnable = QtWidgets.QCheckBox('⑧ 同时命名连接线')
        self.lineNameEnable.setChecked(True)
        grid.addWidget(self.lineNameEnable, row, 0, 1, 4)
        row += 1
        grid.addWidget(QtWidgets.QLabel('命名方向'), row, 0)
        self.lineNameDirectionCombo = self._combo()
        self.lineNameDirectionCombo.addItems(['从汇聚点向末端', '从末端向汇聚点'])
        grid.addWidget(self.lineNameDirectionCombo, row, 1)
        grid.addWidget(QtWidgets.QLabel('连接线输出字段'), row, 2)
        self.lineNameFieldCombo = self._combo(260)
        grid.addWidget(self.lineNameFieldCombo, row, 3)
        row += 1

        buttons = QtWidgets.QHBoxLayout()
        self.okBtn = QtWidgets.QPushButton('确定')
        self.cancelBtn = QtWidgets.QPushButton('取消')
        buttons.addWidget(self.okBtn)
        buttons.addStretch()
        buttons.addWidget(self.cancelBtn)

        main = QtWidgets.QVBoxLayout(self)
        main.setContentsMargins(8, 8, 8, 8)
        main.addLayout(grid)
        main.addSpacing(2)
        main.addLayout(buttons)

    def _vector_layers(self, geometry_type):
        layers = []
        for layer in QgsProject.instance().mapLayers().values():
            try:
                if layer.type() == QgsMapLayer.VectorLayer and QgsWkbTypes.geometryType(layer.wkbType()) == geometry_type:
                    layers.append(layer)
            except Exception:
                continue
        return layers

    def _layer_from_combo(self, combo):
        layer_id = combo.currentData()
        if layer_id:
            layer = QgsProject.instance().mapLayer(str(layer_id))
            if layer is not None:
                return layer
        name = combo.currentText().strip()
        layers = QgsProject.instance().mapLayersByName(name) if name else []
        return layers[0] if layers else None

    def _populate_layers(self):
        for combo in (self.fdtLayerCombo, self.fatLayerCombo, self.clLayerCombo, self.bbLayerCombo):
            combo.clear()
        self.lineLayerCombo.clear()
        for layer in self._vector_layers(QgsWkbTypes.PointGeometry):
            for combo in (self.fdtLayerCombo, self.fatLayerCombo, self.clLayerCombo, self.bbLayerCombo):
                combo.addItem(layer.name(), layer.id())
        for layer in self._vector_layers(QgsWkbTypes.LineGeometry):
            self.lineLayerCombo.addItem(layer.name(), layer.id())
        self._refresh_fdt_fields()
        self._refresh_line_fields()
        self._refresh_modify_fields()

    def _refresh_fdt_fields(self):
        self.fdtFieldCombo.clear()
        layer = self._layer_from_combo(self.fdtLayerCombo)
        if layer is None:
            return
        self.fdtFieldCombo.addItems([field.name() for field in layer.fields()])
        idx = self.fdtFieldCombo.findText('Name')
        if idx >= 0:
            self.fdtFieldCombo.setCurrentIndex(idx)

    def _refresh_line_fields(self):
        self.lineNameFieldCombo.clear()
        layer = self._layer_from_combo(self.lineLayerCombo)
        if layer is None:
            return
        self.lineNameFieldCombo.addItems([field.name() for field in layer.fields()])
        idx = self.lineNameFieldCombo.findText('Name')
        if idx >= 0:
            self.lineNameFieldCombo.setCurrentIndex(idx)

    def _checked_node_layers(self):
        result = []
        for node_type, check_box, combo in (
            ('FAT', self.fatEnable, self.fatLayerCombo),
            ('CL', self.clEnable, self.clLayerCombo),
            ('BB', self.bbEnable, self.bbLayerCombo),
        ):
            if check_box.isChecked():
                layer = self._layer_from_combo(combo)
                if layer is not None:
                    result.append((node_type, layer))
        return result

    def _refresh_modify_fields(self):
        if self._refreshing_fields:
            return
        self._refreshing_fields = True
        try:
            selected = self._checked_node_layers()
            self.modFieldCombo.clear()
            if not selected:
                self.modRadio.setEnabled(False)
                self.modFieldCombo.setEnabled(False)
                return
            field_sets = [{field.name() for field in layer.fields()} for _, layer in selected]
            common_fields = set.intersection(*field_sets) if field_sets else set()
            self.modFieldCombo.addItems(sorted(common_fields, key=str.lower))
            enabled = self.modFieldCombo.count() > 0
            self.modRadio.setEnabled(enabled)
            self.modFieldCombo.setEnabled(enabled and self.modRadio.isChecked())
            if not enabled and self.modRadio.isChecked():
                self.addRadio.setChecked(True)
        finally:
            self._refreshing_fields = False

    def _sample_fdt_name(self):
        layer = self._layer_from_combo(self.fdtLayerCombo)
        field_name = self.fdtFieldCombo.currentText().strip()
        if layer is None or not field_name:
            return 'HB01'
        try:
            for feature in layer.getFeatures():
                value = feature.attribute(field_name)
                if value is not None and str(value).strip():
                    return str(value).strip()
        except Exception:
            pass
        return 'HB01'

    @staticmethod
    def _format_index(prefix, index, mode):
        if mode == '':
            return prefix
        if mode == 'letter':
            value = int(index)
            result = ''
            while value > 0:
                value, rem = divmod(value - 1, 26)
                result = chr(ord('A') + rem) + result
            return f'{prefix}{result}'
        return f'{prefix}{int(index)}'

    def _update_previews(self):
        try:
            base = f'{self.fdtPrefixEdit.text()}{self._sample_fdt_name()}'
            lpart = self._format_index(self.lPrefixEdit.text(), 1, self.lSuffixCombo.currentText())
            fatpart = self._format_index(self.fatPrefixEdit.text(), 1, self.fatSuffixCombo.currentText())
            clpart = self._format_index(self.clPrefixEdit.text(), 1, self.clSuffixCombo.currentText())
            bbpart = f"{self.bbPrefixEdit.text()}5050"
            self.fatPreview.setText(f'{base}{lpart}{fatpart}')
            self.clPreview.setText(f'{base}{lpart}{clpart}')
            self.bbPreview.setText(f'{base}{lpart}{bbpart}')
        except Exception:
            self.fatPreview.setText('HB01_L1_S1')
            self.clPreview.setText('HB01_L1_CL1')
            self.bbPreview.setText('HB01_L1_BB5050')

    def _connect_signals(self):
        self.fdtLayerCombo.currentIndexChanged.connect(self._on_fdt_changed)
        self.lineLayerCombo.currentIndexChanged.connect(self._on_line_changed)
        self.fdtFieldCombo.currentIndexChanged.connect(self._update_previews)
        self.nameOrderCombo.currentIndexChanged.connect(self._update_previews)
        self.sortDirCombo.currentIndexChanged.connect(self._update_previews)
        self.lSuffixCombo.currentIndexChanged.connect(self._update_previews)
        self.fatSuffixCombo.currentIndexChanged.connect(self._update_previews)
        self.clSuffixCombo.currentIndexChanged.connect(self._update_previews)
        self.bbAutoCheck.stateChanged.connect(self._update_previews)
        self.lineNameEnable.stateChanged.connect(self._update_line_mode)
        for edit in (self.lPrefixEdit, self.fdtPrefixEdit, self.fatPrefixEdit, self.clPrefixEdit, self.bbPrefixEdit):
            edit.textChanged.connect(self._update_previews)
        for check_box in (self.fatEnable, self.clEnable, self.bbEnable):
            check_box.stateChanged.connect(self._on_node_config_changed)
        for combo in (self.fatLayerCombo, self.clLayerCombo, self.bbLayerCombo):
            combo.currentIndexChanged.connect(self._on_node_config_changed)
        self.addRadio.toggled.connect(self._update_field_mode)
        self.modRadio.toggled.connect(self._update_field_mode)
        self.okBtn.clicked.connect(self._on_ok)
        self.cancelBtn.clicked.connect(self.reject)

    def _on_fdt_changed(self):
        self._refresh_fdt_fields()
        self._update_previews()

    def _on_line_changed(self):
        self._refresh_line_fields()

    def _on_node_config_changed(self):
        self._refresh_modify_fields()
        self._update_previews()

    def _update_field_mode(self):
        add_mode = self.addRadio.isChecked()
        self.addFieldEdit.setEnabled(add_mode)
        self.modFieldCombo.setEnabled((not add_mode) and self.modRadio.isEnabled())

    def _update_line_mode(self):
        enabled = self.lineNameEnable.isChecked()
        self.lineNameDirectionCombo.setEnabled(enabled)
        self.lineNameFieldCombo.setEnabled(enabled)

    def _load_saved(self):
        ok, cfg = QgsProject.instance().readEntry('site_co_design', 'connectionPointNamingV2')
        if not ok or not cfg:
            return
        try:
            data = dict(cfg)
        except Exception:
            return

        for combo, key in (
            (self.fdtLayerCombo, 'fdt_layer'),
            (self.fdtFieldCombo, 'fdt_field'),
            (self.lineLayerCombo, 'line_layer'),
            (self.nameOrderCombo, 'name_order'),
            (self.sortDirCombo, 'sort_dir'),
            (self.modFieldCombo, 'modify_field'),
            (self.lineNameDirectionCombo, 'line_name_direction'),
            (self.lineNameFieldCombo, 'line_name_field'),
        ):
            self._set_combo_value(combo, data.get(key))
        saved_l_prefix = data.get('l_prefix', '_L')
        self.lPrefixEdit.setText('_L' if str(saved_l_prefix).strip() == 'L' else saved_l_prefix)
        self.lSuffixCombo.setCurrentText(data.get('l_suffix', 'num'))
        self.fdtPrefixEdit.setText(data.get('fdt_prefix', ''))
        self._set_node_row(self.fatEnable, self.fatLayerCombo, data.get('fat_layer'))
        self._set_node_row(self.clEnable, self.clLayerCombo, data.get('cl_layer'))
        self._set_node_row(self.bbEnable, self.bbLayerCombo, data.get('bb_layer'))
        saved_s_prefix = data.get('fat_prefix', '_S')
        self.fatPrefixEdit.setText('_S' if str(saved_s_prefix).strip() == 'S' else saved_s_prefix)
        self.fatSuffixCombo.setCurrentText(data.get('fat_suffix', 'num'))
        self.clPrefixEdit.setText(data.get('cl_prefix', '_CL'))
        self.clSuffixCombo.setCurrentText(data.get('cl_suffix', 'num'))
        self.bbPrefixEdit.setText(data.get('bb_prefix', '_BB'))
        self.bbAutoCheck.setChecked(bool(data.get('bb_auto_type', True)))
        self.addFieldEdit.setText(data.get('add_field', 'Name'))
        self._refresh_modify_fields()
        add_mode = bool(data.get('add_mode', True))
        self.addRadio.setChecked(add_mode)
        if not add_mode and self.modRadio.isEnabled():
            self.modRadio.setChecked(True)
            self._set_combo_value(self.modFieldCombo, data.get('modify_field'))
        self.lineNameEnable.setChecked(bool(data.get('line_name_enabled', True)))

    @staticmethod
    def _set_combo_value(combo, value):
        if value is None:
            return
        text = str(value)
        index = combo.findText(text)
        if index >= 0:
            combo.setCurrentIndex(index)
            return
        for i in range(combo.count()):
            if str(combo.itemData(i)) == text:
                combo.setCurrentIndex(i)
                return

    @staticmethod
    def _set_node_row(check_box, combo, value):
        if value is None:
            return
        text = str(value)
        for i in range(combo.count()):
            if combo.itemText(i) == text or str(combo.itemData(i)) == text:
                combo.setCurrentIndex(i)
                check_box.setChecked(True)
                return

    def _node_payload(self):
        return {
            node_type: {'layer_id': layer.id(), 'layer_name': layer.name()}
            for node_type, layer in self._checked_node_layers()
        }

    def _on_ok(self):
        # site_co_design_impl replaces this handler at import time.
        pass
