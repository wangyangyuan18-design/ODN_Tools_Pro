# -*- coding: utf-8 -*-
"""Low-risk integration hooks for the existing ODN Project wizard."""

import json
import uuid

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


def _after_create(self, original_create):
    """Run the existing creation flow, then register its output globally."""
    original_create()
    path = (getattr(self, "state", {}) or {}).get("project", {}).get("path", "")
    if not path:
        return

    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError, TypeError):
        return

    project = payload.setdefault("project", {})
    if not project.get("project_id"):
        project["project_id"] = str(uuid.uuid4())
    project.setdefault("design_standard", "默认设计标准")
    version = str(project.get("odn_version", "2.0"))
    project.setdefault(
        "node_types",
        ["OLT", "FDT", "FAT", "HP"] if version == "2.0"
        else ["OLT", "FDT", "FAT", "HP", "BB", "SFC CL"]
    )
    params = payload.setdefault("parameters", {})
    # Migrate any older single-distance project value into the two 65 m rules;
    # the New Pole-New Pole rule now has its own 50 m project default.
    legacy_spacing = params.pop("pole_spacing_max", None)
    if legacy_spacing is not None:
        try:
            legacy_spacing = float(legacy_spacing)
        except (TypeError, ValueError):
            legacy_spacing = None
    for key, default in PARAM_DEFAULTS.items():
        if key in params:
            continue
        if key in ("existing_existing_max_distance", "existing_new_max_distance") and legacy_spacing is not None:
            params[key] = legacy_spacing
        else:
            params[key] = default

    # Remove legacy household-capacity parameters introduced by older builds.
    for key in ("fat_ideal_min", "fat_ideal_max", "fat_accept_min", "fat_capacity_max"):
        params.pop(key, None)

    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass
    context.set_current(path, payload=payload)


def install_project_creation_integration(wizard_class):
    """Make every newly created ODN Project the global current project."""
    if getattr(wizard_class, "_odn_context_integrated", False):
        return
    original = wizard_class._create_project

    def wrapped(self):
        return _after_create(self, original.__get__(self, wizard_class))

    wizard_class._create_project = wrapped
    wizard_class._odn_context_integrated = True
