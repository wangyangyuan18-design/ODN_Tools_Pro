# -*- coding: utf-8 -*-
"""Validation-page builder for ODN Project wizard.

Kept in a small module so the wizard's UI construction is explicit and can be
validated independently by CI.
"""

from qgis.PyQt.QtWidgets import QVBoxLayout, QLabel, QListWidget, QGroupBox


def build_validation_page(wizard):
    """Build and return page ④ for an OdnProjectWizard instance."""
    page = wizard.__class__._make_validation_page_base()
    layout = QVBoxLayout(page)
    layout.setSpacing(10)

    intro = QLabel(
        "④ 项目检查　—　创建前再次检查工程角色、几何类型和字段绑定。\n"
        "存在 ✕ 错误时不能创建项目；⚠ 为提示，不阻止创建。"
    )
    intro.setStyleSheet("font-weight:bold;")
    intro.setWordWrap(True)
    layout.addWidget(intro)

    summary_box = QGroupBox("检查结果")
    summary_layout = QVBoxLayout(summary_box)
    wizard.validation_summary = QLabel("尚未执行检查")
    wizard.validation_summary.setWordWrap(True)
    summary_layout.addWidget(wizard.validation_summary)
    layout.addWidget(summary_box)

    wizard.validation_list = QListWidget()
    wizard.validation_list.setMinimumHeight(300)
    layout.addWidget(wizard.validation_list, 1)

    return page


def install_validation_page(wizard_class):
    """Install the validation-page implementation onto the wizard class.

    The helper avoids duplicating UI code in the main project module while
    preserving the public method name expected by the wizard.
    """
    if not hasattr(wizard_class, "_make_validation_page_base"):
        from qgis.PyQt.QtWidgets import QWidget
        wizard_class._make_validation_page_base = staticmethod(QWidget)
    wizard_class._build_validation_page = build_validation_page
