# -*- coding: utf-8 -*-
"""ODN Project workspace: open project, inspect bindings and run project checks."""

import json
import os

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QListWidget, QListWidgetItem, QFileDialog, QMessageBox,
    QGroupBox, QGridLayout
)
from qgis.core import QgsProject, QgsWkbTypes, QgsMapLayer

from .odn_project import OdnProjectSchema


class OdnProjectWorkspace(QDialog):
    """Main entry point after an ODN Project has been created."""

    def __init__(self, iface, project_path=None, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.project_path = project_path or ""
        self.payload = None
        self.setWindowTitle("ODN Project · 工作台")
        self.resize(980, 700)
        self.setMinimumSize(880, 620)
        self._build_ui()
        if self.project_path:
            self._load_project(self.project_path)

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)

        header = QHBoxLayout()
        self.title_label = QLabel("ODN Project · 工作台")
        self.title_label.setStyleSheet("font-size:18px;font-weight:bold;")
        header.addWidget(self.title_label)
        header.addStretch()
        self.status_label = QLabel("未加载项目")
        self.status_label.setStyleSheet("color:#666;")
        header.addWidget(self.status_label)
        root.addLayout(header)

        path_row = QHBoxLayout()
        self.path_label = QLabel("项目文件：—")
        self.path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        path_row.addWidget(self.path_label, 1)
        open_btn = QPushButton("打开 .odn")
        open_btn.clicked.connect(self._browse_project)
        path_row.addWidget(open_btn)
        root.addLayout(path_row)

        summary = QGroupBox("项目概览")
        grid = QGridLayout(summary)
        self.project_name_label = QLabel("—")
        self.version_label = QLabel("—")
        self.layer_count_label = QLabel("—")
        self.error_count_label = QLabel("—")
        grid.addWidget(QLabel("项目名称"), 0, 0)
        grid.addWidget(self.project_name_label, 0, 1)
        grid.addWidget(QLabel("ODN 版本"), 0, 2)
        grid.addWidget(self.version_label, 0, 3)
        grid.addWidget(QLabel("已绑定角色"), 1, 0)
        grid.addWidget(self.layer_count_label, 1, 1)
        grid.addWidget(QLabel("检查错误"), 1, 2)
        grid.addWidget(self.error_count_label, 1, 3)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        root.addWidget(summary)

        self.check_list = QListWidget()
        root.addWidget(self.check_list, 1)

        buttons = QHBoxLayout()
        self.check_btn = QPushButton("重新检查项目")
        self.layer_btn = QPushButton("图层管理")
        self.design_btn = QPushButton("开始方案3设计")
        self.refresh_btn = QPushButton("刷新 QGIS 图层")
        self.save_btn = QPushButton("保存项目")
        close_btn = QPushButton("关闭")
        self.check_btn.clicked.connect(self._run_check)
        self.layer_btn.clicked.connect(self._open_layer_manager)
        self.design_btn.clicked.connect(self._start_scheme3)
        self.refresh_btn.clicked.connect(self._refresh_after_qgis_change)
        self.save_btn.clicked.connect(self._save)
        close_btn.clicked.connect(self.accept)
        for btn in (self.check_btn, self.layer_btn, self.design_btn, self.refresh_btn, self.save_btn, close_btn):
            buttons.addWidget(btn)
        root.addLayout(buttons)

        self._set_enabled(False)

    def _set_enabled(self, enabled):
        for btn in (self.check_btn, self.layer_btn, self.design_btn, self.refresh_btn, self.save_btn):
            btn.setEnabled(enabled)

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
        self.payload = payload
        self.path_label.setText("项目文件：" + path)
        self._update_overview()
        self._set_enabled(True)
        self._run_check()

    def _update_overview(self):
        project = self.payload.get("project", {}) if self.payload else {}
        registry = self.payload.get("layer_registry", {}) if self.payload else {}
        self.title_label.setText("ODN Project · " + (project.get("name") or "工作台"))
        self.project_name_label.setText(project.get("name") or "—")
        self.version_label.setText("ODN " + str(project.get("odn_version") or "—"))
        self.layer_count_label.setText(str(len(registry)))
        self.status_label.setText("● 已加载")
        self.status_label.setStyleSheet("color:#2e7d32;font-weight:bold;")

    @staticmethod
    def _geometry_matches(layer, wanted):
        g = QgsWkbTypes.geometryType(layer.wkbType())
        return (
            (wanted == "point" and g == QgsWkbTypes.PointGeometry)
            or (wanted == "line" and g == QgsWkbTypes.LineGeometry)
            or (wanted == "polygon" and g == QgsWkbTypes.PolygonGeometry)
        )

    def _run_check(self):
        self.check_list.clear()
        if not self.payload:
            self.error_count_label.setText("—")
            return

        errors = 0
        warnings = 0
        registry = self.payload.get("layer_registry", {})
        fields = self.payload.get("field_registry", {})
        version = self.payload.get("project", {}).get("odn_version", "2.0")

        def add(symbol, text):
            nonlocal errors, warnings
            item = QListWidgetItem(f"{symbol}  {text}")
            if symbol == "✕":
                errors += 1
            elif symbol == "⚠":
                warnings += 1
            self.check_list.addItem(item)

        add("✓", f"ODN Version：{version}")
        if self.payload.get("project", {}).get("name"):
            add("✓", "项目名称已设置")
        else:
            add("✕", "项目名称为空")

        # Check every bound role against the current QGIS project using layer ID first.
        for role, entry in registry.items():
            info = OdnProjectSchema.role_info(role)
            if not info:
                add("⚠", f"{role}：项目文件包含未知工程角色")
                continue
            layer = QgsProject.instance().mapLayer(entry.get("layer_id", ""))
            if layer is None:
                add("✕", f"{role}：绑定图层不存在，可通过“图层管理”重新绑定")
                continue
            if not self._geometry_matches(layer, info[1]):
                add("✕", f"{role}：当前图层几何类型不符合工程角色")
                continue
            add("✓", f"{role}：{layer.name()} 绑定正常")

            if info[1] == "point":
                mapping = fields.get(role, {})
                name_field = mapping.get("名称")
                if not name_field:
                    add("✕", f"{role}：缺少“名称”字段绑定")
                elif layer.fields().indexOf(name_field) < 0:
                    add("✕", f"{role}：名称字段“{name_field}”在当前图层不存在")
                else:
                    add("✓", f"{role}：名称 → {name_field}")
                if role == "HP":
                    qty = mapping.get("户数/住宅数量")
                    if not qty:
                        add("✕", "HP：缺少“户数/住宅数量”字段绑定")
                    elif layer.fields().indexOf(qty) < 0:
                        add("✕", f"HP：户数字段“{qty}”在当前图层不存在")
                    else:
                        add("✓", f"HP：户数/住宅数量 → {qty}")

        # Required roles for the selected ODN version.
        required_roles = ["OLT", "FDT", "FAT", "HP", "Pole Edge", "Feeder Cable", "Distribution Cable"]
        if version == "2.1":
            required_roles.extend(["BB", "SFC CL"])
        for role in required_roles:
            if role not in registry:
                add("✕", f"{role}：工程必需角色未绑定")

        if "Existing Pole" not in registry and "New Pole" not in registry:
            add("✕", "Existing Pole / New Pole：至少需要一个杆层")
        elif "Existing Pole" in registry and "New Pole" in registry:
            add("✓", "Existing Pole / New Pole：两个杆层均已绑定")
        else:
            add("✓", "Existing Pole / New Pole：已满足至少一个杆层要求")

        # Optional roles are deliberately informational, not errors.
        for role in ("CL", "Drop Cable", "FAT Boundary"):
            if role not in registry:
                add("⚠", f"{role}：未绑定（可后续添加）")

        self.error_count_label.setText(str(errors))
        if errors:
            self.error_count_label.setStyleSheet("color:#b71c1c;font-weight:bold;")
            self.status_label.setText(f"⚠ 检查发现 {errors} 个错误")
            self.status_label.setStyleSheet("color:#b71c1c;font-weight:bold;")
        elif warnings:
            self.error_count_label.setStyleSheet("color:#b26a00;font-weight:bold;")
            self.status_label.setText(f"● 检查通过，{warnings} 个提示")
            self.status_label.setStyleSheet("color:#b26a00;font-weight:bold;")
        else:
            self.error_count_label.setStyleSheet("color:#2e7d32;font-weight:bold;")
            self.status_label.setText("● 项目检查通过")
            self.status_label.setStyleSheet("color:#2e7d32;font-weight:bold;")

        self.design_btn.setEnabled(errors == 0)

    def _refresh_after_qgis_change(self):
        self._run_check()

    def _save(self):
        if not self.payload or not self.project_path:
            return
        try:
            with open(self.project_path, "w", encoding="utf-8") as f:
                json.dump(self.payload, f, ensure_ascii=False, indent=2)
        except OSError as exc:
            QMessageBox.critical(self, "保存失败", str(exc))
            return
        self.status_label.setText("● 项目已保存")
        self._run_check()

    def _open_layer_manager(self):
        from .odn_project import OdnProjectLayerManager
        dlg = OdnProjectLayerManager(self.iface, project_path=self.project_path, parent=self)
        dlg.exec_()
        self._load_project(self.project_path)

    def _start_scheme3(self):
        try:
            from .scheme3_manual_link_planner import Scheme3Dialog
            self.scheme3_dialog = Scheme3Dialog(self.iface, self.iface.mainWindow())
            self.scheme3_dialog.show()
            self.scheme3_dialog.raise_()
            self.scheme3_dialog.activateWindow()
        except Exception as exc:
            QMessageBox.critical(self, "方案3", f"打开方案3失败：\n{exc}")


ODNProjectWorkspace = OdnProjectWorkspace
