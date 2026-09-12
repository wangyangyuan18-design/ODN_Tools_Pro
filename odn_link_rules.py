# -*- coding: utf-8 -*-
"""Authoritative engineering rules for ODN Link Design.

The values below are project-schema defaults. Operational code reads the
active ODN Project parameters; these constants are used only when a project
is new/legacy and does not yet contain the rule fields.
"""

FDT_MAX_LINKS = 8
MAX_FATS_PER_LINK = 4
RULE_VERSION = 1


def default_parameters():
    return {
        "fdt_max_links": FDT_MAX_LINKS,
        "max_fats_per_link": MAX_FATS_PER_LINK,
        "link_rule_version": RULE_VERSION,
    }


def migrate_parameters(parameters):
    """Normalize legacy Link capacity defaults to the current project schema."""
    params = parameters if isinstance(parameters, dict) else {}
    version = params.get("link_rule_version")

    # 4/4 was the old Project Config default. A project without the new rule
    # marker is migrated once to the current engineering rule 8/4.
    if version != RULE_VERSION:
        params["fdt_max_links"] = FDT_MAX_LINKS
        params["max_fats_per_link"] = MAX_FATS_PER_LINK
        params["link_rule_version"] = RULE_VERSION
    else:
        try:
            params["fdt_max_links"] = max(1, int(params.get("fdt_max_links", FDT_MAX_LINKS)))
        except (TypeError, ValueError):
            params["fdt_max_links"] = FDT_MAX_LINKS
        try:
            params["max_fats_per_link"] = max(1, int(params.get("max_fats_per_link", MAX_FATS_PER_LINK)))
        except (TypeError, ValueError):
            params["max_fats_per_link"] = MAX_FATS_PER_LINK
    return params


def install_project_config_defaults():
    """Keep Project Config UI and newly-created projects on the same rule source."""
    from . import odn_project_config as config
    config.PARAM_DEFAULTS["fdt_max_links"] = FDT_MAX_LINKS
    config.PARAM_DEFAULTS["max_fats_per_link"] = MAX_FATS_PER_LINK

    original_load = getattr(config.OdnProjectConfigDialog, "_load_current", None)
    if original_load is not None and not getattr(config.OdnProjectConfigDialog, "_link_rules_installed", False):
        def _load_current_with_rules(self):
            original_load(self)
            if self.payload is None:
                return
            before = dict(self.payload.get("parameters") or {})
            migrate_parameters(self.payload.setdefault("parameters", {}))
            params = self.payload["parameters"]
            self.fdt_max_links.setValue(int(params["fdt_max_links"]))
            self.max_fats_per_link.setValue(int(params["max_fats_per_link"]))
            if before != params:
                self.status_label.setText("当前项目：链路容量规则已迁移为 8 Link / 4 FAT")

        config.OdnProjectConfigDialog._load_current = _load_current_with_rules
        config.OdnProjectConfigDialog._link_rules_installed = True


def migrate_project_file(path):
    """Upgrade an existing .odn file in place when opened by the plugin."""
    import json
    if not path:
        return False
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if payload.get("format") != "ODN Project":
            return False
        params = dict(payload.get("parameters") or {})
        old = dict(params)
        migrate_parameters(params)
        if params == old:
            return False
        payload["parameters"] = params
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        return True
    except (OSError, ValueError, TypeError):
        return False
