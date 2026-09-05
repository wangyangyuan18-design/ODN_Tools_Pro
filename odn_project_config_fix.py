# -*- coding: utf-8 -*-
"""Compatibility hook for the unified ODN Project configuration dialog."""

from qgis.core import QgsWkbTypes


def _serialize_layer(layer):
    """Serialize a bound QGIS layer for the ODN Project layer registry."""
    return {
        "layer_id": layer.id(),
        "display_name": layer.name(),
        "provider": layer.providerType(),
        "source": layer.source(),
        "geometry": QgsWkbTypes.displayString(layer.wkbType()),
        "crs": layer.crs().authid(),
    }


def install_project_config_fix(dialog_class):
    """Install the missing serializer without changing the unified UI design."""
    if getattr(dialog_class, "_odn_serializer_installed", False):
        return
    dialog_class._serialize_layer = staticmethod(_serialize_layer)
    dialog_class._odn_serializer_installed = True
