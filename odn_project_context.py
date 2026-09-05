# -*- coding: utf-8 -*-
"""Global current-project context for ODN Tools Pro.

The ODN Project is the application-wide design context.  QGIS project files
are associated with an ODN Project through QgsSettings so reopening the same
QGIS project can restore the last ODN Project without silently rewriting the
QGIS .qgz file.
"""

import json
import os

from qgis.core import QgsProject, QgsSettings


_CURRENT_PATH_KEY = "ODNToolsPro/current_project"
_RECENT_PATHS_KEY = "ODNToolsPro/recent_projects"
_QGIS_MAP_PREFIX = "ODNToolsPro/qgis_project_map/"
_MAX_RECENT = 12

_CURRENT = {"path": "", "payload": None}


def _norm(path):
    return os.path.normcase(os.path.abspath(path)) if path else ""


def _load_file(path):
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError, TypeError):
        return None
    if payload.get("format") != "ODN Project":
        return None
    return payload


def _recent_paths():
    raw = QgsSettings().value(_RECENT_PATHS_KEY, [])
    if isinstance(raw, str):
        raw = [x for x in raw.split("|") if x]
    if not isinstance(raw, (list, tuple)):
        raw = []
    result = []
    for path in raw:
        path = str(path)
        if path and path not in result:
            result.append(path)
    return result


def _write_recent(path):
    path = _norm(path)
    paths = [path] + [p for p in _recent_paths() if _norm(p) != path]
    QgsSettings().setValue(_RECENT_PATHS_KEY, paths[:_MAX_RECENT])
    QgsSettings().setValue(_CURRENT_PATH_KEY, path)


def _associate_with_qgis(path):
    qgis_path = QgsProject.instance().fileName()
    if qgis_path:
        QgsSettings().setValue(_QGIS_MAP_PREFIX + _norm(qgis_path), _norm(path))


def load_project(path, make_current=True):
    payload = _load_file(path)
    if payload is None:
        return None
    path = _norm(path)
    if make_current:
        _CURRENT["path"] = path
        _CURRENT["payload"] = payload
        _write_recent(path)
        _associate_with_qgis(path)
    return payload


def save_current(payload=None, path=None):
    target = _norm(path or _CURRENT.get("path", ""))
    data = payload if payload is not None else _CURRENT.get("payload")
    if not target or not data:
        return False
    try:
        folder = os.path.dirname(target)
        if folder and not os.path.isdir(folder):
            os.makedirs(folder)
        with open(target, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except OSError:
        return False
    return load_project(target, make_current=True) is not None


def set_current(path, payload=None):
    if payload is not None:
        path = _norm(path)
        if not path:
            return False
        _CURRENT["path"] = path
        _CURRENT["payload"] = payload
        _write_recent(path)
        _associate_with_qgis(path)
        return True
    return load_project(path, make_current=True) is not None


def clear_current():
    _CURRENT["path"] = ""
    _CURRENT["payload"] = None


def current_path():
    return _CURRENT.get("path", "")


def current_payload():
    return _CURRENT.get("payload")


def current_project_name():
    payload = current_payload() or {}
    return payload.get("project", {}).get("name", "")


def recent_projects():
    result = []
    for path in _recent_paths():
        payload = _load_file(path)
        if payload is not None:
            result.append({
                "path": _norm(path),
                "name": payload.get("project", {}).get("name", ""),
                "version": payload.get("project", {}).get("odn_version", ""),
            })
    return result


def detect_project_for_current_qgis():
    """Return the project associated with the current QGIS project, if any."""
    qgis_path = QgsProject.instance().fileName()
    if qgis_path:
        mapped = QgsSettings().value(_QGIS_MAP_PREFIX + _norm(qgis_path), "")
        if mapped and _load_file(str(mapped)) is not None:
            return _norm(str(mapped))
    last = QgsSettings().value(_CURRENT_PATH_KEY, "")
    if last and _load_file(str(last)) is not None:
        return _norm(str(last))
    recent = recent_projects()
    return recent[0]["path"] if recent else ""


def initialize():
    path = detect_project_for_current_qgis()
    if path:
        load_project(path, make_current=True)
    else:
        clear_current()
    return current_payload()


def project_layer(payload, role):
    entry = (payload or {}).get("layer_registry", {}).get(role, {})
    return QgsProject.instance().mapLayer(entry.get("layer_id", ""))


def project_layer_status(payload, role):
    entry = (payload or {}).get("layer_registry", {}).get(role, {})
    layer = project_layer(payload, role)
    if layer is not None:
        return layer, "ok"
    if entry:
        return None, "missing"
    return None, "unbound"


def require_project(parent=None, feature_name="该功能"):
    """Show a clear message when a feature is invoked without a current project."""
    payload = current_payload()
    if payload:
        return payload
    from qgis.PyQt.QtWidgets import QMessageBox
    QMessageBox.warning(
        parent,
        "ODN Project",
        f"当前没有活动的 ODN Project，无法使用“{feature_name}”。\n\n"
        "请先在【项目管理】中打开或新建 ODN Project。"
    )
    return None
