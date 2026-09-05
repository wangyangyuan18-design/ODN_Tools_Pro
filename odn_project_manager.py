# -*- coding: utf-8 -*-
"""Project management entry point for ODN Tools Pro."""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QListWidget, QListWidgetItem, QMessageBox
)
from qgis.core import QgsProject

from . import odn_project_context as context
from .odn_project import OdnProjectWizard
from .odn_project_config import open_project_config


class OdnProjectManager(QDialog):
    """Switch, create and inspect the globally active ODN Project."""

    def __init__(self, iface, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.setWindowTitle("ODN Tools Pro · 项目管理")
        self.resize(760, 560)
        self._build_ui()
        self._refresh()

    def _build_ui(self):
        root = QVBoxLayout(self)
        header = QHBoxLayout()
        title = QLabel("ODN Tools Pro · 项目管理")
        title.setStyleSheet("font-size:18px;font-weight:bold;")
        header.addWidget(title)
        header.addStretch()
        self.current_label = QLabel("当前项目：无")
        self.current_label.setStyleSheet("font-weight:bold;")
        header.addWidget(self.current_label)
        root.addLayout(header)

        info = QLabel(
            "当前 ODN Project 是整个插件的全局设计上下文。\n"
            "链路设计、接入点命名、网络检查等功能都会直接使用这里显示的项目。"
        )
        info.setStyleSheet("color:#666;padding:6px 0;")
        root.addWidget(info)

        self.detail = QLabel()
        self.detail.setWordWrap(True)
        root.addWidget(self.detail)

        root.addWidget(QLabel("最近使用的 ODN Project："))
        self.recent_list = QListWidget()
        root.addWidget(self.recent_list, 1)
        self.recent_list.itemDoubleClicked.connect(lambda _: self._open_selected())

        buttons = QHBoxLayout()
        self.new_btn = QPushButton("新建项目")
        self.open_btn = QPushButton("打开 / 更换项目")
        self.config_btn = QPushButton("项目配置")
        self.open_selected_btn = QPushButton("加载选中项目")
        close_btn = QPushButton("关闭")
        self.new_btn.clicked.connect(self._new_project)
        self.open_btn.clicked.connect(self._open_project)
        self.config_btn.clicked.connect(self._config)
        self.open_selected_btn.clicked.connect(self._open_selected)
        close_btn.clicked.connect(self.accept)
        for btn in (self.new_btn, self.open_btn, self.config_btn, self.open_selected_btn, close_btn):
            buttons.addWidget(btn)
        root.addLayout(buttons)

    def _refresh(self):
        name = context.current_project_name()
        path = context.current_path()
        payload = context.current_payload() or {}
        project = payload.get("project", {})
        if name:
            self.current_label.setText("当前项目：" + name)
            self.current_label.setStyleSheet("color:#2e7d32;font-weight:bold;")
            self.detail.setText(
                f"ODN {project.get('odn_version', '—')}　|　"
                f"设计标准：{project.get('design_standard', '默认设计标准')}\n"
                f"项目文件：{path or '—'}"
            )
            self.config_btn.setEnabled(True)
        else:
            self.current_label.setText("当前项目：无")
            self.current_label.setStyleSheet("color:#b26a00;font-weight:bold;")
            self.detail.setText("尚未设置活动 ODN Project。请新建或打开一个项目。")
            self.config_btn.setEnabled(False)

        self.recent_list.clear()
        for item in context.recent_projects():
            row = QListWidgetItem(
                f"{item['name'] or '未命名项目'}   |   ODN {item['version']}\n{item['path']}"
            )
            row.setData(Qt.UserRole, item["path"])
            self.recent_list.addItem(row)

    def _new_project(self):
        dlg = OdnProjectWizard(self.iface, self)
        if dlg.exec_() == QDialog.Accepted:
            # The integration hook installed at plugin startup makes the newly
            # created file the global current project.
            self._refresh()
            QMessageBox.information(self, "ODN Project", "新项目已成为当前项目。")

    def _open_project(self):
        from qgis.PyQt.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(
            self, "打开 / 更换 ODN Project", "", "ODN Project (*.odn)"
        )
        if path:
            if context.set_current(path):
                self._refresh()
            else:
                QMessageBox.warning(self, "打开失败", "无法读取有效的 ODN Project 文件。")

    def _open_selected(self):
        item = self.recent_list.currentItem()
        if not item:
            return
        path = item.data(Qt.UserRole)
        if context.set_current(path):
            self._refresh()
        else:
            QMessageBox.warning(self, "打开失败", "该项目文件不存在、无法读取或格式无效。")

    def _config(self):
        if not context.require_project(self, "项目配置"):
            return
        open_project_config(self.iface, self)
        self._refresh()


def initialize_project_manager_context():
    """Restore the current/recent project when the QGIS plugin is loaded."""
    return context.initialize()
