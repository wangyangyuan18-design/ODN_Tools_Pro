# -*- coding: utf-8 -*-
"""ODN Project V1 - project creation, binding and layer management."""

import json
import os

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QGridLayout,
    QLabel, QLineEdit, QPushButton, QComboBox, QRadioButton,
    QButtonGroup, QStackedWidget, QGroupBox, QScrollArea, QWidget,
    QListWidget, QListWidgetItem, QMessageBox, QFileDialog
)
from qgis.core import QgsProject, QgsMapLayer, QgsWkbTypes


class LayerChoice:
    """Small helper carrying a QGIS layer and its display metadata."""

    def __init__(self, layer):
        self.layer = layer

    def label(self):
        source = self.layer.source() or ""
        if len(source) > 78:
            source = "..." + source[-75:]
        geom = QgsWkbTypes.displayString(self.layer.wkbType())
        return f"{self.layer.name()}  |  {geom}  |  {source}"


class OdnProjectSchema:
    """Central semantic definition used by creation and later management."""

    ROLE_GROUPS = [
        ("网络节点", [
            ("OLT", "point", True, "ODN 2.0 / 2.1"),
            ("FDT", "point", True, "ODN 2.0 / 2.1"),
            ("FAT", "point", True, "ODN 2.0 / 2.1"),
            ("HP", "point", True, "ODN 2.0 / 2.1"),
            ("CL", "point", False, "后续按需添加"),
            ("BB", "point", True, "仅 ODN 2.1"),
            ("SFC CL", "point", True, "仅 ODN 2.1"),
        ]),
        ("物理网络", [
            ("Existing Pole", "point", False, "Existing / New 至少一个"),
            ("New Pole", "point", False, "Existing / New 至少一个"),
            ("Pole Edge", "line", True, "ODN 2.0 / 2.1"),
        ]),
        ("光缆", [
            ("Feeder Cable", "line", True, "ODN 2.0 / 2.1"),
            ("Distribution Cable", "line", True, "ODN 2.0 / 2.1"),
            ("Drop Cable", "line", False, "后续按需添加"),
        ]),
        ("辅助图层", [
            ("FAT Boundary", "polygon", False, "可选"),
        ]),
    ]

    @classmethod
    def role_info(cls, role):
        for _, roles in cls.ROLE_GROUPS:
            for item in roles:
                if item[0] == role:
                    return item
        return None

    @classmethod
    def all_roles(cls):
        return [item[0] for _, roles in cls.ROLE_GROUPS for item in roles]


class OdnProjectWizard(QDialog):
    """ODN Project creation wizard.

    Only core data required to establish the project blocks creation.
    Optional roles such as CL and Drop Cable can be added later.
    """

    ROLE_GROUPS = OdnProjectSchema.ROLE_GROUPS

    def __init__(self, iface, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.setWindowTitle("新建 ODN Project")
        self.resize(900, 700)
        self.setMinimumSize(820, 620)

        # Persistent wizard state: UI pages may be rebuilt without losing choices.
        self.state = {
            "project": {"name": "", "odn_version": "2.0", "path": ""},
            "layer_bindings": {},
            "field_bindings": {},
        }
        self.layer_combos = {}
        self.field_combos = {}
        self.role_rows = {}
        self._validation_errors = 0

        self._build_ui()
        self._refresh_layers()

    # ---------- UI ----------
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)

        header = QHBoxLayout()
        title = QLabel("新建 ODN Project")
        title.setStyleSheet("font-size:18px;font-weight:bold;")
        header.addWidget(title)
        header.addStretch()
        self.step_label = QLabel("① 项目信息")
        self.step_label.setStyleSheet("font-weight:bold;")
        header.addWidget(self.step_label)
        root.addLayout(header)

        self.stack = QStackedWidget()
        self.page_project = self._build_project_page()
        self.page_layers = self._build_layer_page()
        self.page_fields = self._build_field_page()
        self.page_validate = self._build_validation_page()
        for page in (self.page_project, self.page_layers, self.page_fields, self.page_validate):
            self.stack.addWidget(page)
        root.addWidget(self.stack, 1)

        nav = QHBoxLayout()
        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color:#666;")
        nav.addWidget(self.status_label)
        nav.addStretch()
        self.back_btn = QPushButton("< 上一步")
        self.next_btn = QPushButton("下一步 >")
        self.create_btn = QPushButton("创建 ODN Project")
        self.create_btn.setVisible(False)
        self.cancel_btn = QPushButton("关闭")
        self.back_btn.clicked.connect(self._back)
        self.next_btn.clicked.connect(self._next)
        self.create_btn.clicked.connect(self._create_project)
        self.cancel_btn.clicked.connect(self.reject)
        nav.addWidget(self.back_btn)
        nav.addWidget(self.next_btn)
        nav.addWidget(self.create_btn)
        nav.addWidget(self.cancel_btn)
        root.addLayout(nav)

    def _build_project_page(self):
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setSpacing(14)

        box = QGroupBox("① 项目信息")
        form = QFormLayout(box)
        self.project_name = QLineEdit()
        self.project_name.setPlaceholderText("例如：AIRTEL_LAGOS_001")
        self.project_name.textChanged.connect(
            lambda text: self.state["project"].update(name=text)
        )
        form.addRow("项目名称：", self.project_name)

        version_box = QGroupBox("ODN 设计版本")
        vlay = QVBoxLayout(version_box)
        self.v20 = QRadioButton("ODN 2.0")
        self.v20.setChecked(True)
        self.v20_desc = QLabel("OLT → FDT → FAT → HP")
        self.v21 = QRadioButton("ODN 2.1")
        self.v21_desc = QLabel("ODN 2.0 + BB + SFC CL")
        for r, d in ((self.v20, self.v20_desc), (self.v21, self.v21_desc)):
            row = QHBoxLayout()
            row.addWidget(r)
            row.addWidget(d)
            row.addStretch()
            vlay.addLayout(row)
        self.version_group = QButtonGroup(self)
        self.version_group.addButton(self.v20)
        self.version_group.addButton(self.v21)
        self.v20.toggled.connect(self._update_version_ui)
        self.v21.toggled.connect(self._update_version_ui)
        form.addRow("", version_box)

        path_row = QHBoxLayout()
        self.project_path = QLineEdit()
        self.project_path.setPlaceholderText("选择 ODN Project 文件保存位置")
        self.project_path.textChanged.connect(
            lambda text: self.state["project"].update(path=text)
        )
        browse = QPushButton("浏览...")
        browse.clicked.connect(self._browse_project_path)
        path_row.addWidget(self.project_path, 1)
        path_row.addWidget(browse)
        form.addRow("ODN Project：", path_row)
        layout.addWidget(box)

        info = QLabel(
            "ODN Project 保存工程定义、图层绑定、字段绑定和设计上下文。\n"
            "不会强制转换或复制 SHP、GeoJSON、GPKG 等原始数据。\n"
            "CL、Drop Cable 等非创建阶段必需图层，可在项目创建后继续添加。"
        )
        info.setStyleSheet("color:#666;padding:8px;")
        layout.addWidget(info)
        layout.addStretch()
        return w

    def _build_layer_page(self):
        w = QWidget()
        outer = QVBoxLayout(w)
        intro = QLabel(
            "② 图层绑定　—　绑定当前 QGIS 项目中的具体图层。\n"
            "注意：Existing Pole / New Pole 二选一即可；CL、Drop Cable 可后续添加。"
        )
        intro.setStyleSheet("font-weight:bold;")
        outer.addWidget(intro)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        self.layer_layout = QVBoxLayout(container)
        self.layer_layout.setContentsMargins(4, 4, 4, 4)
        self._build_layer_rows()
        scroll.setWidget(container)
        outer.addWidget(scroll, 1)
        return w

    def _build_layer_rows(self):
        self._capture_layer_state()
        while self.layer_layout.count():
            item = self.layer_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.layer_combos.clear()
        self.role_rows.clear()

        is21 = self.v21.isChecked()
        for group_name, roles in self.ROLE_GROUPS:
            visible_roles = []
            for role, geom, required, version_note in roles:
                if role in ("BB", "SFC CL") and not is21:
                    continue
                visible_roles.append((role, geom, required, version_note))
            if not visible_roles:
                continue

            group = QGroupBox(group_name)
            grid = QGridLayout(group)
            grid.setColumnStretch(1, 1)
            grid.addWidget(QLabel("工程角色"), 0, 0)
            grid.addWidget(QLabel("QGIS 图层"), 0, 1)
            grid.addWidget(QLabel("状态"), 0, 2)
            grid.addWidget(QLabel("说明"), 0, 3)
            row = 1
            for role, geom, required, version_note in visible_roles:
                label = QLabel(role)
                combo = QComboBox()
                combo.setMinimumWidth(460)
                combo.currentIndexChanged.connect(lambda _=0, r=role: self._layer_changed(r))
                state = QLabel("-")
                state.setMinimumWidth(100)
                note = QLabel(version_note)
                note.setStyleSheet("color:#777;")
                grid.addWidget(label, row, 0)
                grid.addWidget(combo, row, 1)
                grid.addWidget(state, row, 2)
                grid.addWidget(note, row, 3)
                self.layer_combos[role] = combo
                self.role_rows[role] = (label, combo, state, geom, required)
                row += 1
            self.layer_layout.addWidget(group)
        self.layer_layout.addStretch()

    def _build_field_page(self):
        w = QWidget()
        outer = QVBoxLayout(w)
        intro = QLabel(
            "③ 字段绑定　—　点图层只绑定工程真正使用的字段。\n"
            "名称字段优先自动选择 Name；如果没有 Name 且只有一个字段，则自动选择该字段。\n"
            "HP 另外需要手动选择户数/住宅数量字段。"
        )
        intro.setStyleSheet("font-weight:bold;")
        outer.addWidget(intro)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.field_container = QWidget()
        self.field_layout = QVBoxLayout(self.field_container)
        self.field_layout.setContentsMargins(4, 4, 4, 4)
        scroll.setWidget(self.field_container)
        outer.addWidget(scroll, 1)
        return w

    @staticmethod
    def _default_name_field(layer):
        fields = list(layer.fields())
        for field in fields:
            if field.name().lower() == "name":
                return field.name()
        if len(fields) == 1:
            return fields[0].name()
        return None

    def _field_roles(self):
        roles = []
        is21 = self.v21.isChecked()
        for _, role_defs in self.ROLE_GROUPS:
            for role, geom, _, _ in role_defs:
                if role in ("BB", "SFC CL") and not is21:
                    continue
                if geom == "point":
                    combo = self.layer_combos.get(role)
                    if combo and combo.currentData() is not None:
                        roles.append(role)
        return roles

    def _rebuild_field_rows(self):
        self._capture_field_state()
        while self.field_layout.count():
            item = self.field_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.field_combos.clear()

        for role in self._field_roles():
            layer = self.layer_combos[role].currentData()
            box = QGroupBox(role)
            form = QFormLayout(box)
            role_combos = {}

            name_combo = QComboBox()
            name_combo.addItem("— 未选择 —", None)
            for field in layer.fields():
                name_combo.addItem(field.name(), field.name())
            saved_name = self.state["field_bindings"].get(role, {}).get("名称")
            self._restore_combo(name_combo, saved_name)
            if name_combo.currentData() is None:
                default_name = self._default_name_field(layer)
                if default_name is not None:
                    self._restore_combo(name_combo, default_name)
                    self.state["field_bindings"].setdefault(role, {})["名称"] = default_name
            form.addRow("名称 *：", name_combo)
            role_combos["名称"] = name_combo

            if role == "HP":
                qty_combo = QComboBox()
                qty_combo.addItem("— 请选择户数/住宅数量字段 —", None)
                for field in layer.fields():
                    qty_combo.addItem(field.name(), field.name())
                saved_qty = self.state["field_bindings"].get(role, {}).get("户数/住宅数量")
                self._restore_combo(qty_combo, saved_qty)
                form.addRow("户数/住宅数量 *：", qty_combo)
                role_combos["户数/住宅数量"] = qty_combo

            self.field_combos[role] = role_combos
            self.field_layout.addWidget(box)
        self.field_layout.addStretch()

    @staticmethod
    def _restore_combo(combo, value):
        if value is None:
            return
        idx = combo.findData(value)
        if idx >= 0:
            combo.setCurrentIndex(idx)

    def _capture_layer_state(self):
        for role, combo in self.layer_combos.items():
            layer = combo.currentData()
            if layer is not None:
                self.state["layer_bindings"][role] = layer.id()

    def _capture_field_state(self):
        for role, mappings in self.field_combos.items():
            target = self.state["field_bindings"].setdefault(role, {})
            for semantic, combo in mappings.items():
                value = combo.currentData()
                if value is not None:
                    target[semantic] = value

    # ---------- Layer handling ----------
    def _refresh_layers(self):
        self._capture_layer_state()
        layers = [
            layer for layer in QgsProject.instance().mapLayers().values()
            if layer.type() == QgsMapLayer.VectorLayer
        ]
        for role, combo in self.layer_combos.items():
            previous_id = self.state["layer_bindings"].get(role)
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("— 请选择 QGIS 图层 —", None)
            geom = self.role_rows[role][3]
            for layer in layers:
                if self._geometry_matches(layer, geom):
                    combo.addItem(LayerChoice(layer).label(), layer)
            if previous_id:
                for i in range(combo.count()):
                    layer = combo.itemData(i)
                    if layer is not None and layer.id() == previous_id:
                        combo.setCurrentIndex(i)
                        break
            combo.blockSignals(False)
            self._layer_changed(role)

    @staticmethod
    def _geometry_matches(layer, wanted):
        g = QgsWkbTypes.geometryType(layer.wkbType())
        if wanted == "point":
            return g == QgsWkbTypes.PointGeometry
        if wanted == "line":
            return g == QgsWkbTypes.LineGeometry
        if wanted == "polygon":
            return g == QgsWkbTypes.PolygonGeometry
        return True

    def _layer_changed(self, role):
        if role not in self.layer_combos:
            return
        combo = self.layer_combos[role]
        state = self.role_rows[role][2]
        layer = combo.currentData()
        if layer is None:
            state.setText("⚠ 未绑定")
            state.setStyleSheet("color:#b26a00;")
        else:
            state.setText("✓ 已绑定")
            state.setStyleSheet("color:#2e7d32;")
            self.state["layer_bindings"][role] = layer.id()

    # ---------- Navigation ----------
    def _update_version_ui(self):
        if not hasattr(self, "stack"):
            return
        self.state["project"]["odn_version"] = "2.1" if self.v21.isChecked() else "2.0"
        self._build_layer_rows()
        self._refresh_layers()

    def _next(self):
        idx = self.stack.currentIndex()
        if idx == 0:
            self.state["project"]["name"] = self.project_name.text().strip()
            self.state["project"]["path"] = self.project_path.text().strip()
            if not self.state["project"]["name"]:
                QMessageBox.warning(self, "项目名称", "请输入项目名称。")
                return
            self._set_step(1)
        elif idx == 1:
            self._capture_layer_state()
            self._rebuild_field_rows()
            self._set_step(2)
        elif idx == 2:
            self._capture_field_state()
            self._run_validation()
            self._set_step(3)

    def _back(self):
        idx = self.stack.currentIndex()
        if idx == 2:
            self._capture_field_state()
        elif idx == 1:
            self._capture_layer_state()
        if idx > 0:
            self._set_step(idx - 1)

    def _set_step(self, idx):
        self.stack.setCurrentIndex(idx)
        labels = ["① 项目信息", "② 图层绑定", "③ 字段绑定", "④ 项目检查"]
        self.step_label.setText(labels[idx])
        self.back_btn.setVisible(idx > 0)
        self.next_btn.setVisible(idx < 3)
        self.create_btn.setVisible(idx == 3)
        self.status_label.setText("")

    # ---------- Validation ----------
    def _run_validation(self):
        self._capture_layer_state()
        self._capture_field_state()
        self.validation_list.clear()
        errors = 0
        warnings = 0

        def add(status, text):
            nonlocal errors, warnings
            self.validation_list.addItem(QListWidgetItem(f"{status}  {text}"))
            if status == "✕":
                errors += 1
            elif status == "⚠":
                warnings += 1

        version = self.state["project"]["odn_version"]
        add("✓", f"ODN Version：{version}")
        add("✓", "项目名称已设置")

        for role, (_, _, _, geom, required) in self.role_rows.items():
            layer = self._layer_from_role(role)
            if layer is None:
                if required:
                    add("✕", f"{role}：未绑定 QGIS 图层")
                else:
                    add("⚠", f"{role}：未绑定（可后续添加）")
                continue
            if not self._geometry_matches(layer, geom):
                add("✕", f"{role}：几何类型不符合要求")
            else:
                add("✓", f"{role}：图层已绑定 — {layer.name()}")
            if layer.source():
                add("✓", f"{role}：数据源可定位")
            else:
                add("⚠", f"{role}：数据源信息为空")

        existing = self._layer_from_role("Existing Pole")
        new = self._layer_from_role("New Pole")
        if existing is None and new is None:
            add("✕", "Existing Pole / New Pole：至少绑定一个杆层")
        elif existing is None or new is None:
            add("✓", "Existing Pole / New Pole：已满足至少一个杆层要求")
        else:
            add("✓", "Existing Pole / New Pole：两个杆层均已绑定")

        for role, mappings in self.field_combos.items():
            for semantic, combo in mappings.items():
                value = combo.currentData()
                if value is None:
                    add("✕", f"{role}：未绑定“{semantic}”字段")
                else:
                    add("✓", f"{role}：{semantic} → {value}")

        if errors == 0:
            self.validation_summary.setText(
                f"✓ 项目可以创建　　⚠ {warnings} 个提示" if warnings else "✓ 项目可以创建"
            )
        else:
            self.validation_summary.setText(
                f"✕ 暂不能创建　　错误：{errors}　提示：{warnings}"
            )
        self._validation_errors = errors

    def _layer_from_role(self, role):
        layer_id = self.state["layer_bindings"].get(role)
        if not layer_id:
            return None
        return QgsProject.instance().mapLayer(layer_id)

    # ---------- Save ----------
    def _browse_project_path(self):
        default_name = self.project_name.text().strip() or "ODN_Project"
        path, _ = QFileDialog.getSaveFileName(
            self, "保存 ODN Project", default_name + ".odn", "ODN Project (*.odn)"
        )
        if path:
            if not path.lower().endswith(".odn"):
                path += ".odn"
            self.project_path.setText(path)
            self.state["project"]["path"] = path

    def _create_project(self):
        self._run_validation()
        if self._validation_errors > 0:
            QMessageBox.warning(
                self,
                "项目检查",
                "存在错误。可以点击“上一步”返回修改，之前的图层和字段选择会保留。"
            )
            return

        path = self.state["project"]["path"].strip()
        if not path:
            QMessageBox.warning(self, "项目文件", "请选择 ODN Project 保存位置。")
            return

        project_dir = os.path.dirname(os.path.abspath(path))
        if project_dir and not os.path.exists(project_dir):
            try:
                os.makedirs(project_dir)
            except OSError as exc:
                QMessageBox.critical(self, "保存失败", str(exc))
                return

        layers = {}
        for role in OdnProjectSchema.all_roles():
            layer = self._layer_from_role(role)
            if layer is not None:
                layers[role] = self._serialize_layer(layer)

        self._capture_field_state()
        fields = {
            role: dict(mappings)
            for role, mappings in self.state["field_bindings"].items()
            if mappings
        }

        payload = {
            "format": "ODN Project",
            "version": 1,
            "project": {
                "name": self.state["project"]["name"],
                "odn_version": self.state["project"]["odn_version"],
            },
            "layer_registry": layers,
            "field_registry": fields,
            "network_model": {},
            "design_state": {},
            "parameters": {},
            "operation_history": [],
        }

        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except OSError as exc:
            QMessageBox.critical(self, "保存失败", str(exc))
            return

        QMessageBox.information(
            self,
            "ODN Project Ready",
            f"ODN Project 创建成功。\n\n"
            f"项目：{payload['project']['name']}\n"
            f"版本：ODN {payload['project']['odn_version']}\n\n"
            "后续如需要 CL、Drop Cable 等图层，可使用菜单：\n"
            "ODN Project · 图层管理 → 打开 .odn → 添加图层。"
        )
        self.accept()

    @staticmethod
    def _serialize_layer(layer):
        return {
            "layer_id": layer.id(),
            "display_name": layer.name(),
            "provider": layer.providerType(),
            "source": layer.source(),
            "geometry": QgsWkbTypes.displayString(layer.wkbType()),
            "crs": layer.crs().authid(),
        }


class OdnProjectLayerManager(QDialog):
    """Manage semantic layer bindings after an ODN Project exists."""

    def __init__(self, iface, project_path=None, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.project_path = project_path or ""
        self.payload = None
        self.setWindowTitle("ODN Project · 图层管理")
        self.resize(900, 650)
        self._build_ui()
        if self.project_path:
            self._load_project(self.project_path)

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.addWidget(QLabel("ODN Project · 图层绑定管理"))
        info = QLabel(
            "这里管理的是“工程角色 → QGIS 图层”的绑定，不删除、不复制、不修改原始数据。\n"
            "解除绑定只会从 ODN Project 中移除语义绑定。"
        )
        info.setStyleSheet("color:#666;padding-bottom:6px;")
        root.addWidget(info)

        path_row = QHBoxLayout()
        self.path_edit = QLineEdit(self.project_path)
        browse = QPushButton("打开 .odn")
        browse.clicked.connect(self._browse_project)
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(browse)
        root.addLayout(path_row)

        self.list_widget = QListWidget()
        root.addWidget(self.list_widget, 1)

        buttons = QHBoxLayout()
        self.add_btn = QPushButton("添加图层")
        self.change_btn = QPushButton("修改绑定")
        self.remove_btn = QPushButton("解除绑定")
        self.refresh_btn = QPushButton("刷新")
        self.close_btn = QPushButton("关闭")
        self.add_btn.clicked.connect(self._add_layer)
        self.change_btn.clicked.connect(self._change_layer)
        self.remove_btn.clicked.connect(self._remove_layer)
        self.refresh_btn.clicked.connect(self._refresh_list)
        self.close_btn.clicked.connect(self.accept)
        for btn in (self.add_btn, self.change_btn, self.remove_btn, self.refresh_btn, self.close_btn):
            buttons.addWidget(btn)
        root.addLayout(buttons)

    def _browse_project(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "打开 ODN Project", "", "ODN Project (*.odn)"
        )
        if path:
            self._load_project(path)

    def _load_project(self, path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "打开失败", str(exc))
            return
        if payload.get("format") != "ODN Project":
            QMessageBox.warning(self, "文件类型", "这不是有效的 ODN Project 文件。")
            return
        self.project_path = path
        self.path_edit.setText(path)
        self.payload = payload
        self._refresh_list()

    def _refresh_list(self):
        self.list_widget.clear()
        if not self.payload:
            return
        registry = self.payload.setdefault("layer_registry", {})
        for group_name, roles in OdnProjectSchema.ROLE_GROUPS:
            for role, geom, required, note in roles:
                entry = registry.get(role)
                if entry:
                    layer = QgsProject.instance().mapLayer(entry.get("layer_id", ""))
                    if layer:
                        status = "✓ 当前 QGIS 图层"
                        detail = layer.name()
                    else:
                        status = "⚠ 图层缺失，可重新绑定"
                        detail = entry.get("display_name", "")
                else:
                    status = "— 未绑定"
                    detail = ""
                item = QListWidgetItem(f"{role:<18} | {status:<18} | {detail}")
                item.setData(Qt.UserRole, role)
                self.list_widget.addItem(item)

    def _role_dialog(self, title, initial_role=None):
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.resize(620, 220)
        form = QFormLayout(dlg)

        role_combo = QComboBox()
        for group_name, roles in OdnProjectSchema.ROLE_GROUPS:
            for role, geom, required, note in roles:
                role_combo.addItem(f"{role}  —  {note}", role)
        if initial_role:
            idx = role_combo.findData(initial_role)
            if idx >= 0:
                role_combo.setCurrentIndex(idx)
        form.addRow("工程角色：", role_combo)

        layer_combo = QComboBox()
        layers = [
            layer for layer in QgsProject.instance().mapLayers().values()
            if layer.type() == QgsMapLayer.VectorLayer
        ]
        form.addRow("QGIS 图层：", layer_combo)

        def refill():
            layer_combo.clear()
            role = role_combo.currentData()
            info = OdnProjectSchema.role_info(role)
            geom = info[1] if info else None
            layer_combo.addItem("— 请选择 QGIS 图层 —", None)
            for layer in layers:
                if self._geometry_matches(layer, geom):
                    layer_combo.addItem(LayerChoice(layer).label(), layer)

        role_combo.currentIndexChanged.connect(refill)
        refill()

        buttons = QHBoxLayout()
        ok = QPushButton("绑定")
        cancel = QPushButton("取消")
        ok.clicked.connect(dlg.accept)
        cancel.clicked.connect(dlg.reject)
        buttons.addStretch()
        buttons.addWidget(ok)
        buttons.addWidget(cancel)
        form.addRow("", buttons)

        if dlg.exec_() != QDialog.Accepted:
            return None, None
        return role_combo.currentData(), layer_combo.currentData()

    @staticmethod
    def _geometry_matches(layer, wanted):
        g = QgsWkbTypes.geometryType(layer.wkbType())
        if wanted == "point":
            return g == QgsWkbTypes.PointGeometry
        if wanted == "line":
            return g == QgsWkbTypes.LineGeometry
        if wanted == "polygon":
            return g == QgsWkbTypes.PolygonGeometry
        return True

    def _add_layer(self):
        if not self.payload:
            QMessageBox.warning(self, "ODN Project", "请先打开一个 ODN Project。")
            return
        role, layer = self._role_dialog("添加工程图层")
        if not role or layer is None:
            return
        registry = self.payload.setdefault("layer_registry", {})
        if role in registry:
            QMessageBox.warning(
                self,
                "角色已存在",
                f"“{role}”已经有绑定。请使用“修改绑定”，避免误替换。"
            )
            return
        registry[role] = OdnProjectWizard._serialize_layer(layer)
        self._save()

    def _change_layer(self):
        item = self.list_widget.currentItem()
        if not item or not self.payload:
            return
        role = item.data(Qt.UserRole)
        if role not in self.payload.setdefault("layer_registry", {}):
            QMessageBox.information(self, "修改绑定", "当前角色尚未绑定，请使用“添加图层”。")
            return
        selected_role, layer = self._role_dialog("修改图层绑定", role)
        if not selected_role or layer is None:
            return
        if selected_role != role:
            QMessageBox.warning(self, "操作无效", "修改绑定时不能更换工程角色。")
            return
        self.payload["layer_registry"][role] = OdnProjectWizard._serialize_layer(layer)
        self._save()

    def _remove_layer(self):
        item = self.list_widget.currentItem()
        if not item or not self.payload:
            return
        role = item.data(Qt.UserRole)
        if role not in self.payload.get("layer_registry", {}):
            return
        if QMessageBox.question(
            self, "解除绑定", f"确定解除“{role}”的工程绑定？\n不会删除 QGIS 图层。"
        ) != QMessageBox.Yes:
            return
        del self.payload["layer_registry"][role]
        self.payload.setdefault("field_registry", {}).pop(role, None)
        self._save()

    def _save(self):
        if not self.project_path or not self.payload:
            return
        try:
            with open(self.project_path, "w", encoding="utf-8") as f:
                json.dump(self.payload, f, ensure_ascii=False, indent=2)
        except OSError as exc:
            QMessageBox.critical(self, "保存失败", str(exc))
            return
        self._refresh_list()


ODNProjectWizard = OdnProjectWizard
ODNProjectLayerManager = OdnProjectLayerManager
