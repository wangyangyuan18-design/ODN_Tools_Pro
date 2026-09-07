# -*- coding: utf-8 -*-
"""Single-step undo coordinator for ODN Tools Pro.

The actual edits remain owned by QGIS undo stacks. This module remembers the
most recent layer touched by the plugin so the global plugin action can undo
that operation without pretending to own QGIS' complete undo history.
"""

from qgis.core import QgsMessageLog, Qgis

_LAST_LAYER = None
_LAST_LABEL = ""


def record_layer(layer, label="ODN Tools Pro"):
    global _LAST_LAYER, _LAST_LABEL
    _LAST_LAYER = layer
    _LAST_LABEL = str(label or "ODN Tools Pro")


def clear():
    global _LAST_LAYER, _LAST_LABEL
    _LAST_LAYER = None
    _LAST_LABEL = ""


def undo_last(parent=None):
    layer = _LAST_LAYER
    if layer is None:
        _log("[undo] no plugin operation is available", Qgis.Warning)
        return False
    try:
        stack = layer.undoStack()
        if stack is None or stack.canUndo() is False:
            _log(f"[undo] no undo command available for {_LAST_LABEL}", Qgis.Warning)
            return False
        stack.undo()
        _log(f"[undo] undone: {_LAST_LABEL}")
        clear()
        layer.triggerRepaint()
        return True
    except Exception as exc:
        _log(f"[undo-fail] {type(exc).__name__}:{exc}", Qgis.Critical)
        return False


def _log(message, level=Qgis.Info):
    try:
        QgsMessageLog.logMessage(str(message), "ODN_Tools_Pro / Undo", level)
    except Exception:
        pass
