# -*- coding: utf-8 -*-
"""Unified configuration editor for the current ODN Project."""

import json

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QGridLayout,
    QLabel, QLineEdit, QPushButton, QComboBox, QCheckBox,
    QDoubleSpinBox, QSpinBox, QTabWidget, QWidget, QScrollArea,
    QGroupBox, QMessageBox
)
from qgis.core import QgsProject, QgsMapLayer, QgsWkbTypes

from .odn_project import OdnProjectSchema, LayerChoice
from . import odn_project_context as context


PARAM_DEFAULTS = {
    "fdt_max_links": 4,
    "max_fats_per_link": 4,
    "existing_existing_max_distance": 65.0,
    "existing_new_max_distance": 65.0,
    "new_new_max_distance": 50.0,
    "fat_pole_max_distance": 3.0,
    "bb_return_threshold": 100.0,
}


class OdnProjectConfigDialog(QDialog):
    """Edit the current ODN Project definition, layers and fields."""

    def __init__(self, iface, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.payload = None
        self.setWindowTitle("ODN Tools Pro · 项目配置")
        self.resize(1080, 780)
        self.setMinimumSize(960, 680)
        self.layer_combos = {}
        self.field_combos = {}
        self.role_rows = {}
        self._build_ui()
        self._load_current()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)

        header = QHBoxLayout()
        self.title_label = QLabel("项目配置")
        self.title_label.setStyleSheet("font-size:18px;font-weight:bold;")
        header.addWidget(self.title_label)
        header.addStretch()
        self.status_label = QLabel("无活动项目")
        self.status_label.setStyleSheet("color:#777;")
        header.addWidget(self.status_label)
        root.addLayout(header)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_design_tab(), "① 设计设置")
        self.tabs.addTab(self._build_layers_tab(), "② 图层绑定")
        self.tabs.addTab(self._build_check_tab(), "③ 项目检查")
        root.addWidget(self.tabs, 1)

        buttons = QHBoxLayout()
        self.refresh_btn = QPushButton("刷新 QGIS 图层")
        self.save_btn = QPushButton("保存 / 更新")
        close_btn = QPushButton("关闭")
        self.refresh_btn.clicked.connect(self._refresh_all)
        self.save_btn.clicked.connect(self._save)
        close_btn.clicked.connect(self.reject)
        buttons.addStretch()
        buttons.addWidget(self.refresh_btn)
        buttons.addWidget(self.save_btn)
        buttons.addWidget(close_btn)
        root.addLayout(buttons)

    def _build_design_tab(self):
        w = QWidget()
        outer = QVBoxLayout(w)
        outer.setSpacing(12)

        project_box = QGroupBox("项目")
        pf = QFormLayout(project_box)
        self.project_name = QLineEdit()
        self.standard_combo = QComboBox()
        self.standard_combo.setEditable(True)
        self.standard_combo.addItem("默认设计标准")
        self.version_combo = QComboBox()
        self.version_combo.addItem("ODN 2.0", "2.0")
        self.version_combo.addItem("ODN 2.1", "2.1")
        pf.addRow("项目名称：", self.project_name)
        pf.addRow("ODN 版本：", self.version_combo)
        pf.addRow("设计标准：", self.standard_combo)
        outer.addWidget(project_box)

        node_box = QGroupBox("节点类型")
        nl = QGridLayout(node_box)
        self.node_checks = {}
        nodes = ["OLT", "FDT", "FAT", "HP", "CL", "BB", "SFC CL"]
        for i, role in enumerate(nodes):
            cb = QCheckBox(role)
            self.node_checks[role] = cb
            nl.addWidget(cb, i // 4, i % 4)
        outer.addWidget(node_box)

        param_box = QGroupBox("ODN 参数")
        params_layout = QFormLayout(param_box)
        self.fdt_max_links = self._spin(1, 999, 4)
        self.max_fats_per_link = self._spin(1, 999, 4)
        self.existing_existing_max_distance = self._double_spin(0.01, 99999, 65.0)
        self.existing_new_max_distance = self._double_spin(0.01, 99999, 65.0)
        self.new_new_max_distance = self._double_spin(0.01, 99999, 50.0)
        self.fat_pole_max_distance = self._double_spin(0.01, 9999, 3.0)
        self.bb_return_threshold = self._double_spin(0.01, 999999, 100.0)
        params_layout.addRow("FDT 最大 Link 数：", self.fdt_max_links)
        params_layout.addRow("每条 Link 最大 FAT：", self.max_fats_per_link)
        params_layout.addRow("最大允许距离（米）· Existing Pole-Existing Pole：", self.existing_existing_max_distance)
        params_layout.addRow("最大允许距离（米）· Existing Pole-New Pole：", self.existing_new_max_distance)
        params_layout.addRow("最大允许距离（米）· New Pole-New Pole：", self.new_new_max_distance)
        params_layout.addRow("FAT 挂杆最大距离（m）：", self.fat_pole_max_distance)
        params_layout.addRow("BB 回程长度参考值（m）：", self.bb_return_threshold)
        outer.addWidget(param_box)

        info = QLabel(
            "以上设置属于当前 ODN Project。杆间距采用三类端点组合规则；"
            "超距增点只读取并执行这里保存的规则，不在功能界面重复设置。"
        )
        info.setStyleSheet("color:#666;padding:6px;")
        info.setWordWrap(True)
        outer.addWidget(info)
        outer.addStretch()
        self.version_combo.currentIndexChanged.connect(lambda _=0: self._version_changed())
        return w

    @staticmethod
    def _spin(minimum, maximum, value):
        box = QSpinBox()
        box.setRange(minimum, maximum)
        box.setValue(value)
        return box

    @staticmethod
    def _double_spin(minimum, maximum, value):
        box = QDoubleSpinBox()
        box.setRange(minimum, maximum)
        box.setDecimals(2)
        box.setValue(value)
        return box

    def _build_layers_tab(self):
        w = QWidget()
        outer = QVBoxLayout(w)
        intro = QLabel(
            "工程角色、QGIS 图层和字段统一在这里绑定。"
            "状态后直接显示该点图层使用的字段；线/面图层无需字段绑定。"
        )
        intro.setStyleSheet("font-weight:bold;")
        intro.setWordWrap(True)
        outer.addWidget(intro)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        self.layer_container = QWidget()
        self.layer_layout = QVBoxLayout(self.layer_container)
        self.layer_layout.setContentsMargins(4, 4, 4, 4)
        self.layer_layout.setSpacing(8)
        scroll.setWidget(self.layer_container)
        outer.addWidget(scroll, 1)
        return w

    def _build_check_tab(self):
        w = QWidget()
        outer = QVBoxLayout(w)
        self.check_title = QLabel("项目检查")
        self.check_title.setStyleSheet("font-weight:bold;")
        outer.addWidget(self.check_title)
        self.check_list = QLabel()
        self.check_list.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.check_list.setWordWrap(True)
        self.check_list.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        outer.addWidget(self.check_list, 1)
        return w

    def _load_current(self):
        payload = context.current_payload()
        if payload is None:
            self._set_no_project()
            return
        self.payload = payload
        project = payload.setdefault("project", {})
        self.project_name.setText(project.get("name", ""))
        standard = project.get("design_standard", "默认设计标准")
        if self.standard_combo.findText(standard) < 0:
            self.standard_combo.addItem(standard)
        self.standard_combo.setCurrentText(standard)
        version = str(project.get("odn_version", "2.0"))
        idx = self.version_combo.findData(version)
        self.version_combo.setCurrentIndex(idx if idx >= 0 else 0)

        nodes = project.get("node_types") or []
        defaults = {"OLT", "FDT", "FAT", "HP"} if version == "2.0" else {"OLT", "FDT", "FAT", "HP", "BB", "SFC CL"}
        for role, cb in self.node_checks.items():
            cb.setChecked(role in (set(nodes) if nodes else defaults))

        params = payload.setdefault("parameters", {})
        # Migrate old project parameter keys to the new distance-rule schema.
        legacy_spacing = params.pop("pole_spacing_max", None)
        if legacy_spacing is not None:
            try:
                legacy_spacing = float(legacy_spacing)
            except (TypeError, ValueError):
                legacy_spacing = None
        self._set_value(self.fdt_max_links, params, "fdt_max_links", PARAM_DEFAULTS["fdt_max_links"])
        self._set_value(self.max_fats_per_link, params, "max_fats_per_link", PARAM_DEFAULTS["max_fats_per_link"])
        self._set_value(
            self.existing_existing_max_distance,
            params,
            "existing_existing_max_distance",
            legacy_spacing if legacy_spacing is not None else PARAM_DEFAULTS["existing_existing_max_distance"],
        )
        self._set_value(
            self.existing_new_max_distance,
            params,
            "existing_new_max_distance",
            legacy_spacing if legacy_spacing is not None else PARAM_DEFAULTS["existing_new_max_distance"],
        )
        self._set_value(
            self.new_new_max_distance,
            params,
            "new_new_max_distance",
            PARAM_DEFAULTS["new_new_max_distance"],
        )
        self._set_value(self.fat_pole_max_distance, params, "fat_pole_max_distance", PARAM_DEFAULTS["fat_pole_max_distance"])
        self._set_value(self.bb_return_threshold, params, "bb_return_threshold", PARAM_DEFAULTS["bb_return_threshold"])

        self._refresh_layer_rows()
        self._update_status()
        self._run_check_text()

    @staticmethod
    def _set_value(widget, mapping, key, default):
        value = mapping.get(key, default)
        try:
            widget.setValue(value)
        except (TypeError, ValueError):
            widget.setValue(default)

    def _set_no_project(self):
        self.status_label.setText("无活动项目")
        self.status_label.setStyleSheet("color:#b26a00;font-weight:bold;")
        self.save_btn.setEnabled(False)
        self.check_list.setText("当前没有活动 ODN Project。请先通过【项目管理】新建或打开项目。")

    def _update_status(self):
        name = (self.payload.get("project", {}).get("name") if self.payload else "") or "未命名项目"
        version = (self.payload.get("project", {}).get("odn_version") if self.payload else "") or "—"
        self.title_label.setText("项目配置 · " + name)
        self.status_label.setText(f"当前项目：{name}   |   ODN {version}")
        self.status_label.setStyleSheet("color:#2e7d32;font-weight:bold;")
        self.save_btn.setEnabled(True)

    @staticmethod
    def _geometry_matches(layer, wanted):
        g = QgsWkbTypes.geometryType(layer.wkbType())
        return ((wanted == "point" and g == QgsWkbTypes.PointGeometry)
                or (wanted == "line" and g == QgsWkbTypes.LineGeometry)
                or (wanted == "polygon" and g == QgsWkbTypes.PolygonGeometry))

    def _refresh_layer_rows(self):
        previous_ids = {role: combo.currentData().id() if combo.currentData() is not None else None
                        for role, combo in self.layer_combos.items()}
        while self.layer_layout.count():
            item = self.layer_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.layer_combos.clear()
        self.field_combos.clear()
        self.role_rows.clear()

        version = self.version_combo.currentData() or "2.0"
        registry = (self.payload or {}).setdefault("layer_registry", {})
        fields = (self.payload or {}).setdefault("field_registry", {})
        layers = [l for l in QgsProject.instance().mapLayers().values() if l.type() == QgsMapLayer.VectorLayer]

        for group_name, roles in OdnProjectSchema.ROLE_GROUPS:
            visible = [(r, g, req, note) for r, g, req, note in roles
                       if not (r in ("BB", "SFC CL") and version != "2.1")]
            if not visible:
                continue

            box = QGroupBox(group_name)
            grid = QGridLayout(box)
            grid.setColumnMinimumWidth(0, 120)
            grid.setColumnStretch(1, 1)
            grid.setColumnMinimumWidth(2, 90)
            grid.setColumnStretch(3, 1)
            grid.setHorizontalSpacing(10)
            grid.setVerticalSpacing(6)
            grid.addWidget(QLabel("工程角色"), 0, 0)
            grid.addWidget(QLabel("QGIS 图层"), 0, 1)
            grid.addWidget(QLabel("状态"), 0, 2)
            grid.addWidget(QLabel("字段"), 0, 3)

            for row, (role, geom, req, note) in enumerate(visible, 1):
                role_label = QLabel(role)
                combo = QComboBox()
                combo.setMinimumWidth(420)
                combo.addItem("— 未绑定 / 不使用 —", None)
                for layer in layers:
                    if self._geometry_matches(layer, geom):
                        combo.addItem(LayerChoice(layer).label(), layer)

                saved = registry.get(role, {}).get("layer_id")
                target_id = previous_ids.get(role) or saved
                if target_id:
                    for i in range(combo.count()):
                        layer = combo.itemData(i)
                        if layer is not None and layer.id() == target_id:
                            combo.setCurrentIndex(i)
                            break

                state = QLabel()
                field_widget = self._build_inline_field_widget(role, combo, fields)
                grid.addWidget(role_label, row, 0)
                grid.addWidget(combo, row, 1)
                grid.addWidget(state, row, 2)
                grid.addWidget(field_widget, row, 3)

                self.layer_combos[role] = combo
                self.role_rows[role] = (state, geom, req)
                combo.currentIndexChanged.connect(lambda _=0, r=role: self._layer_changed(r))
                self._layer_changed(role, refresh_fields=False)
                self._refresh_inline_fields(role, combo)

            self.layer_layout.addWidget(box)
        self.layer_layout.addStretch()

    def _build_inline_field_widget(self, role, layer_combo, fields):
        widget = QWidget()
        widget.setObjectName("ODNInlineFieldContainer")
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.field_combos[role] = {}
        if role not in ("OLT", "FDT", "FAT", "HP", "CL", "BB", "SFC CL"):
            layout.addWidget(QLabel("—"))
            return widget

        name_combo = QComboBox()
        name_combo.setMinimumWidth(150)
        name_combo.addItem("名称字段", None)
        qty_combo = None
        if role == "HP":
            qty_combo = QComboBox()
            qty_combo.setMinimumWidth(150)
            qty_combo.addItem("户数/住宅数量", None)
        self.field_combos[role]["名称"] = name_combo
        if qty_combo is not None:
            self.field_combos[role]["户数/住宅数量"] = qty_combo
        layout.addWidget(name_combo)
        if qty_combo is not None:
            layout.addWidget(qty_combo)
        layout.addStretch()
        return widget

    def _refresh_inline_fields(self, role, layer_combo):
        layer = layer_combo.currentData()
        combos = self.field_combos.get(role, {})
        saved = (self.payload or {}).get("field_registry", {}).get(role, {})
        for semantic, combo in combos.items():
            combo.blockSignals(True)
            combo.clear()
            if semantic == "名称":
                combo.addItem("名称字段", None)
            else:
                combo.addItem("户数/住宅数量", None)
            if layer is not None:
                for field in layer.fields():
                    combo.addItem(field.name(), field.name())
                wanted = saved.get(semantic)
                if wanted:
                    idx = combo.findData(wanted)
                    if idx >= 0:
                        combo.setCurrentIndex(idx)
                elif semantic == "名称":
                    for field in layer.fields():
                        if field.name().lower() == "name":
                            combo.setCurrentIndex(combo.findData(field.name()))
                            break
                    else:
                        if len(list(layer.fields())) == 1:
                            combo.setCurrentIndex(1)
            combo.blockSignals(False)
            try:
                combo.currentIndexChanged.disconnect()
            except (TypeError, RuntimeError):
                pass
            combo.currentIndexChanged.connect(
                lambda _=0, r=role, s=semantic: self._field_changed(r, s)
            )
        self._update_field_state(role)

    def _update_field_state(self, role):
        return

    def _layer_changed(self, role, refresh_fields=True):
        combo = self.layer_combos.get(role)
        row = self.role_rows.get(role)
        if combo is None or row is None or not self.payload:
            return
        layer = combo.currentData()
        state = row[0]
        state.setText("✓ 已绑定" if layer is not None else "— 未绑定")
        state.setStyleSheet("color:#2e7d32;" if layer is not None else "color:#b26a00;")
        registry = self.payload.setdefault("layer_registry", {})
        field_registry = self.payload.setdefault("field_registry", {})
        if layer is None:
            registry.pop(role, None)
            field_registry.pop(role, None)
        else:
            registry[role] = self._serialize_layer(layer)
        if refresh_fields:
            self._refresh_inline_fields(role, combo)

    def _field_changed(self, role, semantic):
        combo = self.field_combos.get(role, {}).get(semantic)
        if combo is None or not self.payload:
            return
        mapping = self.payload.setdefault("field_registry", {}).setdefault(role, {})
        value = combo.currentData()
        if value is None:
            mapping.pop(semantic, None)
        else:
            mapping[semantic] = value

    def _version_changed(self):
        if not self.payload:
            return
        version = self.version_combo.currentData() or "2.0"
        self.payload.setdefault("project", {})["odn_version"] = version
        defaults = {"OLT", "FDT", "FAT", "HP"} if version == "2.0" else {"OLT", "FDT", "FAT", "HP", "BB", "SFC CL"}
        current = set(self.payload.get("project", {}).get("node_types") or [])
        if not current:
            current = defaults
        for role, cb in self.node_checks.items():
            cb.setChecked(role in current)
        self._refresh_layer_rows()
        self._run_check_text()

    def _refresh_all(self):
        if not self.payload:
            self._load_current()
            return
        self._refresh_layer_rows()
        self._run_check_text()

    def _collect_payload(self):
        project = self.payload.setdefault("project", {})
        project["name"] = self.project_name.text().strip()
        project["odn_version"] = self.version_combo.currentData() or "2.0"
        project["design_standard"] = self.standard_combo.currentText().strip() or "默认设计标准"
        project["node_types"] = [role for role, cb in self.node_checks.items() if cb.isChecked()]
        params = self.payload.setdefault("parameters", {})
        params.pop("pole_spacing_max", None)
        for key in ("fat_ideal_min", "fat_ideal_max", "fat_accept_min", "fat_capacity_max"):
            params.pop(key, None)
        params.update({
            "fdt_max_links": self.fdt_max_links.value(),
            "max_fats_per_link": self.max_fats_per_link.value(),
            "existing_existing_max_distance": self.existing_existing_max_distance.value(),
            "existing_new_max_distance": self.existing_new_max_distance.value(),
            "new_new_max_distance": self.new_new_max_distance.value(),
            "fat_pole_max_distance": self.fat_pole_max_distance.value(),
            "bb_return_threshold": self.bb_return_threshold.value(),
        })

        for role, combos in self.field_combos.items():
            mapping = self.payload.setdefault("field_registry", {}).setdefault(role, {})
            for semantic, combo in combos.items():
                value = combo.currentData()
                if value is None:
                    mapping.pop(semantic, None)
                else:
                    mapping[semantic] = value
            if not mapping:
                self.payload.setdefault("field_registry", {}).pop(role, None)

    def _run_check_text(self):
        if not self.payload:
            return 0
        self._collect_payload()
        registry = self.payload.get("layer_registry", {})
        version = self.payload.get("project", {}).get("odn_version", "2.0")
        fields = self.payload.get("field_registry", {})
        required = ["OLT", "FDT", "FAT", "HP", "Pole Edge", "Feeder Cable", "Distribution Cable"]
        if version == "2.1":
            required.extend(["BB", "SFC CL"])
        messages = []
        errors = 0
        messages.append(f"ODN Version：{version}")
        for role in required:
            if role not in registry:
                messages.append(f"✕ {role}：未绑定")
                errors += 1
                continue
            layer = context.project_layer(self.payload, role)
            if layer is None:
                messages.append(f"✕ {role}：绑定图层当前不存在")
                errors += 1
            else:
                messages.append(f"✓ {role}：{layer.name()}")

        if "Existing Pole" not in registry and "New Pole" not in registry:
            messages.append("✕ Existing Pole / New Pole：至少绑定一个")
            errors += 1
        else:
            messages.append("✓ Existing Pole / New Pole：杆层要求满足")

        for role, info in ((r, OdnProjectSchema.role_info(r)) for r in registry):
            if not info or info[1] != "point":
                continue
            mapping = fields.get(role, {})
            if not mapping.get("名称"):
                messages.append(f"✕ {role}：缺少名称字段")
                errors += 1
            if role == "HP" and not mapping.get("户数/住宅数量"):
                messages.append("✕ HP：缺少户数/住宅数量字段")
                errors += 1

        for role in ("CL", "Drop Cable", "FAT Boundary"):
            if role not in registry:
                messages.append(f"⚠ {role}：未绑定（可后续添加）")

        params = self.payload.get("parameters", {})
        messages.append(
            "参数：FDT 最大 Link={0}，每条 Link 最大 FAT={1}，"
            "EE={2:g}m，EN={3:g}m，NN={4:g}m，FAT 挂杆距离={5:g}m，BB 回程参考={6:g}m".format(
                params.get("fdt_max_links", 4),
                params.get("max_fats_per_link", 4),
                params.get("existing_existing_max_distance", 65.0),
                params.get("existing_new_max_distance", 65.0),
                params.get("new_new_max_distance", 50.0),
                params.get("fat_pole_max_distance", 3.0),
                params.get("bb_return_threshold", 100.0),
            )
        )

        self.check_title.setText("项目检查：通过" if errors == 0 else f"项目检查：发现 {errors} 个问题")
        self.check_title.setStyleSheet(
            "font-weight:bold;color:#2e7d32;" if errors == 0
            else "font-weight:bold;color:#b71c1c;"
        )
        self.check_list.setText("\n".join(messages))
        return errors

    def _serialize_layer(self, layer):
        return {
            "layer_id": layer.id(),
            "display_name": layer.name(),
            "provider": layer.providerType(),
            "source": layer.source(),
            "geometry": QgsWkbTypes.displayString(layer.wkbType()),
            "crs": layer.crs().authid(),
        }

    def _save(self):
        if not self.payload:
            return
        self._collect_payload()
        errors = self._run_check_text()
        if errors:
            QMessageBox.warning(self, "项目检查", "保存前发现必需配置问题，请先修正。\n\n可选图层未绑定不会阻止保存。")
            return
        path = context.current_path()
        if not path:
            QMessageBox.warning(self, "ODN Project", "当前项目没有有效的 .odn 文件路径。")
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(self.payload, fh, ensure_ascii=False, indent=2)
        except OSError as exc:
            QMessageBox.critical(self, "保存失败", str(exc))
            return
        context.set_current(path, payload=self.payload)
        self._update_status()
        QMessageBox.information(self, "ODN Project", "项目配置已保存/更新，当前插件配置已同步。")


def open_project_config(iface, parent=None):
    dlg = OdnProjectConfigDialog(iface, parent=parent)
    dlg.exec_()
    return dlg
