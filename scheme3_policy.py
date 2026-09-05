# -*- coding: utf-8 -*-
"""Scheme 3 policy adjustments driven by the active ODN Project."""


def install_scheme3_policy(dialog_class):
    """Use the active Project's max FAT per Link; minimum is always 1."""
    original_build_ui = dialog_class._build_ui

    def build_ui(self):
        original_build_ui(self)
        try:
            from . import odn_project_context as context
            payload = context.current_payload() or {}
            value = int((payload.get("parameters", {}) or {}).get("max_fats_per_link", 4))
        except (TypeError, ValueError, AttributeError):
            value = 4
        value = max(1, value)
        self.max_fats.setRange(1, 999)
        self.max_fats.setValue(value)
        self.max_fats.setToolTip("由当前 ODN Project 控制；最小值为 1")

    dialog_class._build_ui = build_ui
