# -*- coding: utf-8 -*-
"""Interactive ODN Link Design.

The active ODN Project is the sole source of operational layers and design
limits. Link planning is saved independently from Distribution Cable writing.
"""
from math import sqrt
import json

from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import Qt, QSettings
from qgis.core import (
    QgsCoordinateTransform, QgsFeature, QgsGeometry, QgsPointXY, QgsProject,
    QgsRectangle, QgsSpatialIndex, QgsWkbTypes,
)
from qgis.gui import QgsMapTool, QgsRubberBand

from . import odn_project_context as context
from .odn_project_routing import OdnProjectRouteEngine

SETTINGS_KEY = "ODNToolsPro/LinkDesign"


def _payload(dialog):
    return context.current_payload() or getattr(dialog, "_odn_project_payload", None) or {}


def _required_int(dialog, key):
    value = (_payload(dialog).get("parameters", {}) or {}).get(key)
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if value >= 1 else None


def _max_links(dialog):
    return _required_int(dialog, "fdt_max_links")


def _max_fats(dialog):
    return _required_int(dialog, "max_fats_per_link")


def _assigned_fat_ids(dialog, include_draft=True, exclude_index=None):
    result = set()
    for index, design in enumerate(dialog._designs):
        if exclude_index is not None and index == exclude_index:
            continue
        for item in design.get("nodes", []):
            try:
                result.add(int(item[0]))
            except (TypeError, ValueError, IndexError):
                continue
    if include_draft:
        for item in dialog._sequence:
            if item[0] == "FAT":
                try:
                    result.add(int(item[1]))
                except (TypeError, ValueError, IndexError):
                    continue
    return result


def _planned_fats(dialog):
    return len(_assigned_fat_ids(dialog, include_draft=True))


def _total_fats(dialog):
    layer = context.project_layer(_payload(dialog), "FAT")
    return int(layer.featureCount()) if layer is not None else 0


def _next_link(dialog, fdt_label):
    limit = _max_links(dialog)
    if limit is None:
        return None
    used = {d.get("link") for d in dialog._designs if d.get("fdt") == fdt_label}
    for index in range(1, limit + 1):
        candidate = f"L{index}"
        if candidate not in used:
            return candidate
    return None


def _project_state_key():
    path = QgsProject.instance().fileName() or "__UNSAVED_PROJECT__"
    path = str(path).replace("\\", "/")
    return f"{SETTINGS_KEY}/state/{path}"


class LinkDesignDialog(QtWidgets.QDialog):
    """Compact Link planning dialog with persistent saved designs."""

    def __init__(self, iface, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.setWindowTitle("链路设计")
        self.resize(520, 410)
        self.setMinimumWidth(480)
        self.setModal(False)

        self._designs = []
        self._sequence = []
        self._direction = None
        self._current_fdt = None
        self._current_fdt_id = None
        self._current_link = None
        self._editing_index = None
        self._draw_active = False
        self._engine = None
        self._tool = None
        self._saved_bands = []

        self._build_ui()
        self._load_saved_state()
        self._refresh_ui()

    def _load_saved_state(self):
        state = None
        try:
            raw = QSettings().value(_project_state_key(), "")
            if raw:
                state = json.loads(str(raw))
        except Exception:
            state = None

        if isinstance(state, dict):
            designs = state.get("designs", [])
            draft = state.get("draft")
            self._designs = designs if isinstance(designs, list) else []
            if isinstance(draft, dict):
                self._restore_draft(draft)
        else:
            ok, value = QgsProject.instance().readEntry(SETTINGS_KEY, "designs", "")
            if ok and value:
                try:
                    self._designs = json.loads(value)
                except Exception:
                    self._designs = []

    def _restore_draft(self, draft):
        try:
            self._sequence = [
                (str(item[0]), int(item[1]), str(item[2]))
                for item in draft.get("sequence", []) if len(item) >= 3
            ]
        except Exception:
            self._sequence = []
        self._direction = draft.get("direction")
        self._current_fdt = draft.get("current_fdt")
        try:
            value = draft.get("current_fdt_id")
            self._current_fdt_id = int(value) if value is not None else None
        except (TypeError, ValueError):
            self._current_fdt_id = None
        self._current_link = draft.get("current_link")
        value = draft.get("editing_index")
        try:
            self._editing_index = int(value) if value is not None else None
        except (TypeError, ValueError):
            self._editing_index = None

    def _draft_payload(self):
        if not self._sequence:
            return None
        return {
            "sequence": [list(item) for item in self._sequence],
            "direction": self._direction,
            "current_fdt": self._current_fdt,
            "current_fdt_id": self._current_fdt_id,
            "current_link": self._current_link,
            "editing_index": self._editing_index,
        }

    def _persist_state(self):
        state = {"designs": self._designs, "draft": self._draft_payload()}
        raw = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
        try:
            settings = QSettings()
            settings.setValue(_project_state_key(), raw)
            settings.sync()
        except Exception:
            pass
        try:
            QgsProject.instance().writeEntry(
                SETTINGS_KEY, "designs", json.dumps(self._designs, ensure_ascii=False)
            )
            QgsProject.instance().writeEntry(
                SETTINGS_KEY, "draft", json.dumps(self._draft_payload(), ensure_ascii=False)
            )
        except Exception:
            pass

    def _clear_draft(self):
        self._sequence = []
        self._direction = None
        self._current_fdt = None
        self._current_fdt_id = None
        self._current_link = None
        self._editing_index = None

    def _build_ui(self):
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(5)

        title = QtWidgets.QLabel("链路设计")
        font = title.font()
        font.setBold(True)
        font.setPointSize(13)
        title.setFont(font)
        root.addWidget(title)

        self.plan_label = QtWidgets.QLabel("① 规划中")
        self.fdt_label = QtWidgets.QLabel("② 规划中 FDT：— | Link：—")
        self.link_count_label = QtWidgets.QLabel("已规划链路：0")
        self.planned_fat_label = QtWidgets.QLabel("已规划 FAT：0")
        self.total_fat_label = QtWidgets.QLabel("③ 总 FAT：0，已设计 FAT：0")
        for widget in (
            self.plan_label, self.fdt_label, self.link_count_label,
            self.planned_fat_label, self.total_fat_label,
        ):
            root.addWidget(widget)

        line = QtWidgets.QFrame()
        line.setFrameShape(QtWidgets.QFrame.HLine)
        line.setFrameShadow(QtWidgets.QFrame.Sunken)
        root.addWidget(line)

        details = QtWidgets.QGridLayout()
        details.setHorizontalSpacing(8)
        details.setVerticalSpacing(4)
        self.current_path_title = QtWidgets.QLabel("当前路径：")
        self.current_path_label = QtWidgets.QLabel("—")
        self.current_path_label.setWordWrap(True)
        self.distance_title = QtWidgets.QLabel("各段距离：")
        self.distance_label = QtWidgets.QLabel("—")
        self.distance_label.setWordWrap(True)
        self.segment_title = QtWidgets.QLabel("当前段：")
        self.segment_label = QtWidgets.QLabel("—")
        self.route_title = QtWidgets.QLabel("Pole Edge：")
        self.route_label = QtWidgets.QLabel("—")
        self.route_label.setWordWrap(True)
        details.addWidget(self.current_path_title, 0, 0)
        details.addWidget(self.current_path_label, 0, 1)
        details.addWidget(self.distance_title, 1, 0)
        details.addWidget(self.distance_label, 1, 1)
        details.addWidget(self.segment_title, 2, 0)
        details.addWidget(self.segment_label, 2, 1)
        details.addWidget(self.route_title, 3, 0)
        details.addWidget(self.route_label, 3, 1)
        details.setColumnStretch(1, 1)
        root.addLayout(details)

        row = QtWidgets.QHBoxLayout()
        self.start_btn = QtWidgets.QPushButton("开始规划")
        self.save_btn = QtWidgets.QPushButton("保存规划")
        self.done_btn = QtWidgets.QPushButton("已完成设计")
        self.exit_btn = QtWidgets.QPushButton("退出设计")
        for button in (self.start_btn, self.save_btn, self.done_btn, self.exit_btn):
            button.setMinimumHeight(28)
            row.addWidget(button)
        root.addLayout(row)

        self.status = QtWidgets.QLabel("状态：等待开始规划")
        self.status.setFrameStyle(QtWidgets.QFrame.StyledPanel | QtWidgets.QFrame.Sunken)
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        self.start_btn.clicked.connect(self.start_design)
        self.save_btn.clicked.connect(self.save_current_link)
        self.done_btn.clicked.connect(self.open_completed_designs)
        self.exit_btn.clicked.connect(self.exit_design)
        self.save_btn.setEnabled(False)

    def _prepare_engine(self):
        payload = _payload(self)
        fdt = context.project_layer(payload, "FDT")
        fat = context.project_layer(payload, "FAT")
        edge = context.project_layer(payload, "Pole Edge")
        missing = [
            role for role, layer in (("FDT", fdt), ("FAT", fat), ("Pole Edge", edge))
            if layer is None
        ]
        if missing:
            QtWidgets.QMessageBox.warning(
                self, "链路设计",
                "当前项目缺少必要图层绑定：\n\n"
                + "\n".join(f"• {role}" for role in missing)
                + "\n\n请先在【项目配置】中修正。"
            )
            return None
        if QgsWkbTypes.geometryType(fdt.wkbType()) != QgsWkbTypes.PointGeometry:
            QtWidgets.QMessageBox.warning(self, "链路设计", "项目配置中的 FDT 不是点图层。")
            return None
        if QgsWkbTypes.geometryType(fat.wkbType()) != QgsWkbTypes.PointGeometry:
            QtWidgets.QMessageBox.warning(self, "链路设计", "项目配置中的 FAT 不是点图层。")
            return None
        if QgsWkbTypes.geometryType(edge.wkbType()) != QgsWkbTypes.LineGeometry:
            QtWidgets.QMessageBox.warning(self, "链路设计", "项目配置中的 Pole Edge 不是线图层。")
            return None
        if _max_links(self) is None or _max_fats(self) is None:
            QtWidgets.QMessageBox.warning(
                self, "链路设计",
                "当前 ODN Project 缺少有效的“FDT 最大 Link 数”或“每条 Link 最大 FAT”参数。\n\n"
                "请先在【项目配置 → 设计设置】中完成配置。"
            )
            return None
        params = payload.get("parameters", {}) or {}
        try:
            attach = float(params["fat_pole_max_distance"])
        except (KeyError, TypeError, ValueError):
            QtWidgets.QMessageBox.warning(
                self, "链路设计",
                "当前 ODN Project 缺少有效的 FAT 挂杆最大距离参数，请先在【项目配置】中配置。"
            )
            return None
        try:
            engine = OdnProjectRouteEngine(self.iface, payload, attach)
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "链路设计", f"无法初始化 Pole Edge 路径引擎：\n{exc}")
            return None
        if not engine.ready():
            QtWidgets.QMessageBox.warning(self, "链路设计", "当前项目的 FDT、FAT 或 Pole Edge 无法使用。")
            return None
        return engine

    def _activate_map_tool(self):
        if self._engine is None:
            self._engine = self._prepare_engine()
        if self._engine is None:
            return False
        self._tool = LinkDesignMapTool(self.iface, self._engine, self)
        self.iface.mapCanvas().setMapTool(self._tool)
        self._draw_active = True
        self.start_btn.setEnabled(False)
        self._tool.start()
        return True

    def start_design(self):
        self._engine = self._prepare_engine()
        if self._engine is None:
            return
        if self._draw_active:
            self.status.setText("状态：正在规划中，请继续点击地图上的 FDT/FAT。")
        elif self._sequence:
            self.status.setText("状态：已恢复未完成规划，请继续点击 FDT/FAT。")
        else:
            self._direction = None
            self._current_fdt = None
            self._current_fdt_id = None
            self._current_link = None
            self._editing_index = None
            self.status.setText("状态：规划中——请选择 FDT。")
        if self._activate_map_tool():
            self._persist_state()
            self._refresh_ui()

    def _start_from_hit(self, info):
        if info["typ"] == "FDT":
            if self._editing_index is not None and self._current_fdt == info["label"]:
                link = self._current_link
            else:
                link = _next_link(self, info["label"])
            if self._editing_index is None and link is None:
                self.status.setText(
                    f"状态：{info['label']} 已达到项目配置的最大 Link 数：{_max_links(self) or '未配置'}。"
                )
                return False
            self._direction = "FDT_TO_FAT"
            self._current_fdt = info["label"]
            self._current_fdt_id = info["feature_id"]
            self._current_link = link or _next_link(self, info["label"])
            self._sequence = [("FDT", info["feature_id"], info["label"])]
            self.status.setText(f"状态：{info['label']}/{self._current_link}，请继续点击 FAT。")
            self._persist_state()
            self._refresh_ui()
            return True

        if info["typ"] == "FAT":
            if info["feature_id"] in _assigned_fat_ids(
                self, include_draft=False, exclude_index=self._editing_index
            ):
                self.status.setText(f"状态：{info['label']} 已分配到其他 Link，保存时会禁止重复使用。")
                # Planning is still allowed: add it to the current draft.
            if self._current_fdt and not self._sequence:
                next_link = self._current_link or _next_link(self, self._current_fdt)
                if next_link is None:
                    self.status.setText(f"状态：{self._current_fdt} 已达到项目配置的最大 Link 数。")
                    return False
                self._direction = "FDT_TO_FAT"
                self._current_link = next_link
                self._sequence = [("FDT", self._current_fdt_id, self._current_fdt)]
            else:
                self._direction = "FAT_TO_FDT"
                self._sequence = [("FAT", info["feature_id"], info["label"])]
                self.status.setText("状态：已从 FAT 开始，请继续点击 FAT，最后点击 FDT。")
            self._persist_state()
            self._refresh_ui()
            return True
        return False

    def add_fat(self, info):
        if not self._draw_active or info["typ"] != "FAT":
            return
        if not self._sequence:
            self._start_from_hit(info)
            return
        if any(item[0] == "FAT" and item[1] == info["feature_id"] for item in self._sequence):
            self.status.setText(f"状态：{info['label']} 已在当前链路中。")
            return
        max_fats = _max_fats(self)
        current_fats = sum(1 for item in self._sequence if item[0] == "FAT")
        if max_fats is None:
            self.status.setText("状态：当前项目未配置“每条 Link 最大 FAT”。")
            return
        if current_fats >= max_fats:
            self.status.setText(f"状态：当前 Link 已达到项目配置的 FAT 上限：{max_fats}。")
            return
        previous = self._sequence[-1]
        if self._engine is None:
            self._engine = self._prepare_engine()
        route = self._engine.route(previous[0], previous[1], "FAT", info["feature_id"]) if self._engine else None
        if route is None:
            self.status.setText(f"状态：{previous[2]} → {info['label']} 无法沿 Pole Edge 建立完整路径。")
            return
        self._sequence.append(("FAT", info["feature_id"], info["label"]))
        self._persist_state()
        self._refresh_ui()
        if self._tool:
            self._tool.refresh_route_preview()

    def add_fdt(self, info):
        if not self._draw_active or info["typ"] != "FDT" or self._direction != "FAT_TO_FDT" or not self._sequence:
            return
        if self._editing_index is None:
            link = _next_link(self, info["label"])
        else:
            current = self._designs[self._editing_index] if 0 <= self._editing_index < len(self._designs) else None
            link = current.get("link") if current else _next_link(self, info["label"])
        if not link:
            self.status.setText(f"状态：{info['label']} 已达到项目配置的最大 Link 数。")
            return
        previous = self._sequence[-1]
        route = self._engine.route(previous[0], previous[1], "FDT", info["feature_id"])
        if route is None:
            self.status.setText(f"状态：{previous[2]} → {info['label']} 无法沿 Pole Edge 建立完整路径。")
            return
        self._current_fdt = info["label"]
        self._current_fdt_id = info["feature_id"]
        self._current_link = link
        self._sequence.append(("FDT", info["feature_id"], info["label"]))
        self._persist_state()
        self._refresh_ui()

    def _validate_current_design(self):
        if not self._sequence:
            self.status.setText("状态：当前没有可保存的规划。")
            return None
        fats = [item for item in self._sequence if item[0] == "FAT"]
        if not fats:
            self.status.setText("状态：当前链路至少需要 1 个 FAT。")
            return None
        fdt_item = next((item for item in self._sequence if item[0] == "FDT"), None)
        if not fdt_item:
            self.status.setText("状态：当前规划尚未确定 FDT，无法保存。")
            return None
        fdt_label = self._current_fdt or fdt_item[2]
        fdt_id = self._current_fdt_id if self._current_fdt_id is not None else fdt_item[1]
        link = self._current_link or _next_link(self, fdt_label)
        max_fats = _max_fats(self)
        if not link or max_fats is None or len(fats) > max_fats:
            self.status.setText(
                f"状态：当前 Link FAT 数 {len(fats)} 超过项目配置上限 {max_fats or '未配置'}，无法保存。"
            )
            return None

        seen_local = set()
        duplicate_local = []
        for item in fats:
            fid = int(item[1])
            if fid in seen_local:
                duplicate_local.append(item[2])
            seen_local.add(fid)
        if duplicate_local:
            QtWidgets.QMessageBox.warning(
                self, "无法保存规划",
                "当前 Link 内存在重复 FAT：\n\n" + "、".join(duplicate_local)
                + "\n\n可以继续规划，但需要修改后才能保存。"
            )
            self.status.setText("状态：当前 Link 内存在重复 FAT，未保存。")
            return None

        assigned_elsewhere = _assigned_fat_ids(
            self, include_draft=False, exclude_index=self._editing_index
        )
        duplicate_other = [item[2] for item in fats if int(item[1]) in assigned_elsewhere]
        if duplicate_other:
            QtWidgets.QMessageBox.warning(
                self, "无法保存规划",
                "以下 FAT 已经被其他已保存 Link 使用：\n\n"
                + "、".join(duplicate_other)
                + "\n\n当前规划可以继续修改，但不能保存，除非调整 FAT 选择。"
            )
            self.status.setText("状态：存在已被其他 Link 使用的 FAT，当前规划未保存。")
            return None

        if self._engine is None:
            self._engine = self._prepare_engine()
        if self._engine is None:
            return None
        routes = []
        for first, second in zip(self._sequence[:-1], self._sequence[1:]):
            route = self._engine.route(first[0], first[1], second[0], second[1])
            if route is None:
                self.status.setText(f"状态：{first[2]} → {second[2]} 无法沿 Pole Edge 建立完整路径，无法保存。")
                return None
            routes.append(route)

        return {
            "fdt": fdt_label,
            "fdt_id": int(fdt_id),
            "link": link,
            "nodes": [(item[1], item[2]) for item in fats],
            "length": round(sum(item["distance"] for item in routes), 3),
            "sequence": [item[2] for item in self._sequence],
            "sequence_ids": [(item[0], item[1]) for item in self._sequence],
            "direction": self._direction,
            "written": False,
            "segments": [
                {
                    "from": r["from_label"],
                    "to": r["to_label"],
                    "distance": round(r["distance"], 3),
                    "pole_edge_distance": round(r["pole_edge_distance"], 3),
                    "edge_count": len(r["edge_sequence"]),
                    "points": [[float(p.x()), float(p.y())] for p in r["points"]],
                }
                for r in routes
            ],
        }

    def save_current_link(self):
        design = self._validate_current_design()
        if not design:
            return False
        if self._editing_index is not None:
            index = self._editing_index
            if 0 <= index < len(self._designs) and self._designs[index].get("written"):
                QtWidgets.QMessageBox.warning(
                    self, "已写入 Link",
                    "该 Link 已经写入 Distribution Cable 图层，当前版本不能直接覆盖。"
                )
                return False
            if 0 <= index < len(self._designs):
                self._designs[index] = design
            saved_text = f"已更新 {design['fdt']}/{design['link']}"
        else:
            existing_index = next(
                (i for i, d in enumerate(self._designs)
                 if d.get("fdt") == design["fdt"] and d.get("link") == design["link"]),
                None,
            )
            if existing_index is not None:
                if self._designs[existing_index].get("written"):
                    QtWidgets.QMessageBox.warning(self, "已写入 Link", "当前 Link 已经写入图层，不能直接覆盖。")
                    return False
                self._designs[existing_index] = design
            else:
                self._designs.append(design)
            saved_text = f"已保存 {design['fdt']}/{design['link']}"

        saved_fdt = design["fdt"]
        saved_link = design["link"]
        saved_len = design["length"]
        self._clear_draft()
        self._current_fdt = saved_fdt
        self._current_fdt_id = design["fdt_id"]
        self._current_link = _next_link(self, saved_fdt)
        self._persist_state()
        self._refresh_ui()
        self._clear_saved_bands()
        self.status.setText(f"状态：{saved_text}（{saved_len:.1f} m），请继续规划下一条链路。")
        return True

    def load_design_for_edit(self, index):
        try:
            index = int(index)
        except (TypeError, ValueError):
            return False
        if index < 0 or index >= len(self._designs):
            return False
        design = self._designs[index]
        if design.get("written"):
            QtWidgets.QMessageBox.information(
                self, "已写入 Link",
                "该 Link 已经写入 Distribution Cable 图层。\n\n当前版本暂不能直接编辑已写入 Link。"
            )
            return False
        try:
            labels = design.get("sequence", [])
            seq = design.get("sequence_ids", [])
            self._sequence = [
                (str(item[0]), int(item[1]), str(labels[pos]))
                for pos, item in enumerate(seq)
            ]
        except Exception:
            self.status.setText("状态：无法恢复该 Link 的规划数据。")
            return False
        self._direction = design.get("direction") or "FDT_TO_FAT"
        self._current_fdt = str(design.get("fdt", ""))
        try:
            self._current_fdt_id = int(design.get("fdt_id"))
        except (TypeError, ValueError):
            self._current_fdt_id = self._sequence[0][1] if self._sequence else None
        self._current_link = str(design.get("link", ""))
        self._editing_index = index
        self._engine = self._prepare_engine()
        if self._engine is None:
            return False
        self.status.setText(f"状态：正在修改 {self._current_fdt}/{self._current_link}，请调整 FAT 后保存。")
        self._refresh_ui()
        if not self._activate_map_tool():
            return False
        self._persist_state()
        return True

    def open_completed_designs(self):
        dialog = CompletedDesignDialog(self)
        dialog.exec_()
        self._refresh_ui()

    def show_saved_designs(self, entries):
        self._clear_saved_bands()
        self._engine = self._prepare_engine()
        if self._engine is None:
            return
        canvas = self.iface.mapCanvas()
        canvas_crs = canvas.mapSettings().destinationCrs()
        for _, design in entries:
            seq = design.get("sequence_ids", [])
            stored_segments = design.get("segments", [])
            for idx, segment in enumerate(stored_segments):
                try:
                    points = [QgsPointXY(float(p[0]), float(p[1])) for p in segment.get("points", [])]
                except Exception:
                    points = []
                if len(points) < 2 and idx < len(seq) - 1:
                    try:
                        first, second = seq[idx], seq[idx + 1]
                        route = self._engine.route(first[0], int(first[1]), second[0], int(second[1]))
                        points = route["points"] if route else []
                    except Exception:
                        points = []
                if len(points) < 2:
                    continue
                src = self._engine.edge_layer.crs()
                if src != canvas_crs:
                    transform = QgsCoordinateTransform(src, canvas_crs, QgsProject.instance().transformContext())
                    points = [transform.transform(p) for p in points]
                band = QgsRubberBand(canvas, QgsWkbTypes.LineGeometry)
                band.setWidth(5)
                band.setColor(QtWidgets.QApplication.palette().highlight().color())
                band.setToGeometry(QgsGeometry.fromPolylineXY(points), canvas_crs)
                self._saved_bands.append(band)

    def _refresh_ui(self):
        self.link_count_label.setText(f"已规划链路：{len(self._designs)}")
        self.planned_fat_label.setText(f"已规划 FAT：{_planned_fats(self)}")
        self.total_fat_label.setText(
            f"③ 总 FAT：{_total_fats(self)}，已设计 FAT：{_planned_fats(self)}"
        )
        if not self._sequence:
            self.plan_label.setText("① 规划中")
            self.fdt_label.setText(
                f"② 规划中 FDT：{self._current_fdt or '—'} | Link：{self._current_link or '—'}"
            )
            self.current_path_label.setText("—")
            self.distance_label.setText("—")
            self.segment_label.setText("—")
            self.route_label.setText("—")
            self.save_btn.setEnabled(False)
            return
        self.plan_label.setText("① 规划中" + (" · 修改" if self._editing_index is not None else ""))
        self.fdt_label.setText(
            f"② 规划中 FDT：{self._current_fdt or '等待 FDT'} | Link：{self._current_link or '待分配'}"
        )
        self.current_path_label.setText(" - ".join(item[2] for item in self._sequence))
        routes = []
        if self._engine:
            for first, second in zip(self._sequence[:-1], self._sequence[1:]):
                route = self._engine.route(first[0], first[1], second[0], second[1])
                if route:
                    routes.append(route)
        self.distance_label.setText(", ".join(f"{route['distance']:.1f}m" for route in routes) if routes else "—")
        last = routes[-1] if routes else None
        if last:
            self.segment_label.setText(f"{last['from_label']} - {last['to_label']}    {last['distance']:.1f}m")
            self.route_label.setText(f"实际路径 {last['pole_edge_distance']:.1f}m | 边段 {len(last['edge_sequence'])}")
        else:
            self.segment_label.setText("—")
            self.route_label.setText("—")
        fat_count = sum(1 for item in self._sequence if item[0] == "FAT")
        self.save_btn.setEnabled(bool(self._current_fdt and fat_count >= 1))

    def _clear_saved_bands(self):
        canvas = self.iface.mapCanvas()
        for band in self._saved_bands:
            try:
                canvas.scene().removeItem(band)
            except Exception:
                pass
        self._saved_bands = []

    def _stop_tool(self):
        self._draw_active = False
        if self._tool:
            try:
                self._tool.clear_preview_only()
            except Exception:
                pass
            try:
                self.iface.mapCanvas().unsetMapTool(self._tool)
            except Exception:
                pass
            self._tool = None
        self.start_btn.setEnabled(True)

    def exit_design(self):
        self._persist_state()
        self._stop_tool()
        self._clear_saved_bands()
        self.close()

    def closeEvent(self, event):
        self._persist_state()
        self._stop_tool()
        self._clear_saved_bands()
        event.accept()

    def undo_last(self):
        if not self._sequence:
            return
        self._sequence.pop()
        if not self._sequence:
            self._direction = None
            self._current_fdt = None
            self._current_fdt_id = None
            self._current_link = None
        elif self._direction == "FAT_TO_FDT" and self._sequence[-1][0] == "FAT":
            self._current_fdt = None
            self._current_fdt_id = None
            self._current_link = None
        self._persist_state()
        self._refresh_ui()
        if self._tool:
            self._tool.refresh_route_preview()

    def prospective_route(self, info):
        if not self._sequence or not self._engine:
            return None
        if info["typ"] == "FDT" and self._direction != "FAT_TO_FDT":
            return None
        if info["typ"] == "FAT":
            max_fats = _max_fats(self)
            current_fats = sum(1 for item in self._sequence if item[0] == "FAT")
            if max_fats is None or current_fats >= max_fats:
                return None
        previous = self._sequence[-1]
        return self._engine.route(previous[0], previous[1], info["typ"], info["feature_id"])

    def _distribution_cable_layer(self):
        return context.project_layer(_payload(self), "Distribution Cable")

    def write_planned_links(self):
        layer = self._distribution_cable_layer()
        if layer is None:
            QtWidgets.QMessageBox.warning(self, "写入图层", "当前项目没有绑定 Distribution Cable 图层。")
            return False
        pending = [(i, d) for i, d in enumerate(self._designs) if not d.get("written")]
        if not pending:
            QtWidgets.QMessageBox.information(self, "写入图层", "没有待写入的已规划 Link。")
            return True
        if not layer.isEditable() and not layer.startEditing():
            QtWidgets.QMessageBox.warning(self, "写入图层", f"无法进入 Distribution Cable 图层编辑状态：{layer.name()}")
            return False
        try:
            added = 0
            for index, design in pending:
                segments = design.get("segments", [])
                if not segments:
                    raise ValueError(f"{design.get('fdt')}/{design.get('link')} 缺少有效路径段。")
                for segment in segments:
                    points = [QgsPointXY(float(p[0]), float(p[1])) for p in segment.get("points", [])]
                    if len(points) < 2:
                        raise ValueError(
                            f"{design.get('fdt')}/{design.get('link')} 的 {segment.get('from')} → {segment.get('to')} 缺少线形。"
                        )
                    feature = QgsFeature(layer.fields())
                    feature.setGeometry(QgsGeometry.fromPolylineXY(points))
                    # Cable naming/attributes are intentionally left untouched.
                    # Naming happens only after Link topology is finalized.
                    if not layer.addFeature(feature):
                        raise ValueError(f"无法写入 Distribution Cable：{design.get('fdt')}/{design.get('link')}")
                    added += 1
                design["written"] = True
            layer.triggerRepaint()
            self._persist_state()
            self._refresh_ui()
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "写入图层", f"写入 Distribution Cable 失败：\n{exc}")
            return False

        QtWidgets.QMessageBox.information(
            self, "写入图层",
            f"已将 {len(pending)} 条 Link、共 {added} 个线路段写入 Distribution Cable 图层。\n\n"
            "Link 规划数据仍保留在“已完成设计”中。"
        )
        return True


class CompletedDesignDialog(QtWidgets.QDialog):
    """Browser for all saved Link designs, whether planned or written."""

    def __init__(self, main_dialog):
        super().__init__(main_dialog)
        self.main_dialog = main_dialog
        self.setWindowTitle("已完成设计")
        self.resize(420, 440)
        self.setMinimumSize(380, 370)
        self._last_target = None
        self._build_ui()
        self._refresh_tree()

    def _build_ui(self):
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(6)

        title = QtWidgets.QLabel("已完成设计")
        font = title.font()
        font.setBold(True)
        font.setPointSize(12)
        title.setFont(font)
        root.addWidget(title)

        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(["FDT / Link", "状态"])
        self.tree.setColumnWidth(0, 270)
        self.tree.setAlternatingRowColors(True)
        self.tree.itemClicked.connect(self._on_clicked)
        self.tree.itemDoubleClicked.connect(self._on_double_clicked)
        root.addWidget(self.tree, 1)

        self.info = QtWidgets.QLabel("点击 FDT 查看全部 Link；点击 Link 查看该 Link。")
        self.info.setWordWrap(True)
        self.info.setStyleSheet("color:#666;")
        root.addWidget(self.info)

        row = QtWidgets.QHBoxLayout()
        self.modify_btn = QtWidgets.QPushButton("修改选中 Link")
        self.write_btn = QtWidgets.QPushButton("确定并写入图层")
        close_btn = QtWidgets.QPushButton("关闭")
        row.addWidget(self.modify_btn)
        row.addWidget(self.write_btn)
        row.addWidget(close_btn)
        root.addLayout(row)
        self.modify_btn.clicked.connect(self._modify_selected)
        self.write_btn.clicked.connect(self._write_all)
        close_btn.clicked.connect(self.accept)

    def _refresh_tree(self):
        self.tree.clear()
        grouped = {}
        for index, design in enumerate(self.main_dialog._designs):
            grouped.setdefault(str(design.get("fdt", "未知 FDT")), []).append((index, design))
        for fdt in sorted(grouped):
            root = QtWidgets.QTreeWidgetItem([fdt, ""])
            root.setData(0, Qt.UserRole, ("fdt", fdt))
            root.setToolTip(0, "点击显示该 FDT 的全部 Link")
            self.tree.addTopLevelItem(root)
            for index, design in sorted(grouped[fdt], key=lambda x: str(x[1].get("link", ""))):
                status = "已写入" if design.get("written") else "已规划"
                child = QtWidgets.QTreeWidgetItem([str(design.get("link", "L?")), status])
                child.setData(0, Qt.UserRole, ("link", index))
                child.setToolTip(0, " → ".join(design.get("sequence", [])))
                root.addChild(child)
        if self.tree.topLevelItemCount():
            self.tree.topLevelItem(0).setExpanded(False)

    def _on_clicked(self, item, column):
        target = item.data(0, Qt.UserRole)
        if not target:
            return
        self._last_target = target
        if target[0] == "fdt":
            fdt = target[1]
            entries = [(i, d) for i, d in enumerate(self.main_dialog._designs) if d.get("fdt") == fdt]
            self.main_dialog.show_saved_designs(entries)
            self.info.setText(f"{fdt}：已显示全部 {len(entries)} 条 Link。")
        else:
            index = int(target[1])
            if 0 <= index < len(self.main_dialog._designs):
                design = self.main_dialog._designs[index]
                self.main_dialog.show_saved_designs([(index, design)])
                self.info.setText(
                    f"{design.get('fdt', '')}/{design.get('link', '')}："
                    f"{'已写入' if design.get('written') else '已规划'}。"
                )

    def _on_double_clicked(self, item, column):
        target = item.data(0, Qt.UserRole)
        if target and target[0] == "link":
            self._modify_selected()

    def _modify_selected(self):
        current = self.tree.currentItem()
        target = self._last_target or (current.data(0, Qt.UserRole) if current else None)
        if not target or target[0] != "link":
            QtWidgets.QMessageBox.information(self, "修改 Link", "请先选择一个 Link。")
            return
        index = int(target[1])
        if index < 0 or index >= len(self.main_dialog._designs):
            return
        if self.main_dialog.load_design_for_edit(index):
            self.accept()

    def _write_all(self):
        if self.main_dialog.write_planned_links():
            self._refresh_tree()
            if self._last_target:
                self._on_target_refresh(self._last_target)

    def _on_target_refresh(self, target):
        if target[0] == "fdt":
            fdt = target[1]
            entries = [(i, d) for i, d in enumerate(self.main_dialog._designs) if d.get("fdt") == fdt]
            self.main_dialog.show_saved_designs(entries)
        elif target[0] == "link":
            index = int(target[1])
            if 0 <= index < len(self.main_dialog._designs):
                self.main_dialog.show_saved_designs([(index, self.main_dialog._designs[index])])


class LinkDesignMapTool(QgsMapTool):
    """Canvas interaction for project-configured FDT/FAT Link Design."""

    def __init__(self, iface, engine, dialog):
        super().__init__(iface.mapCanvas())
        self.iface = iface
        self.engine = engine
        self.dialog = dialog
        self.canvas = iface.mapCanvas()
        self.setCursor(Qt.CrossCursor)
        self._features = []
        self._index = QgsSpatialIndex()
        self._hover_band = None
        for key, info in engine.points.items():
            feature = QgsFeature()
            feature.setId(key[1])
            feature.setGeometry(QgsGeometry.fromPointXY(info["point"]))
            self._index.addFeature(feature)
            self._features.append((key, info))

    def start(self):
        self.refresh_route_preview()

    def _nearest(self, pos):
        point = self.toMapCoordinates(pos)
        tolerance = self.canvas.mapUnitsPerPixel() * 24
        rect = QgsRectangle(
            point.x() - tolerance, point.y() - tolerance,
            point.x() + tolerance, point.y() + tolerance,
        )
        candidate_ids = self._index.intersects(rect)
        best = None
        for key, info in self._features:
            if key[1] not in candidate_ids:
                continue
            p = info["point"]
            distance = sqrt((p.x() - point.x()) ** 2 + (p.y() - point.y()) ** 2)
            if best is None or distance < best[0]:
                best = (distance, key, info)
        return best

    def canvasReleaseEvent(self, event):
        if event.button() != Qt.LeftButton or not self.dialog._draw_active:
            return
        hit = self._nearest(event.pos())
        if not hit:
            return
        _, _, info = hit
        if not self.dialog._sequence:
            self.dialog._start_from_hit(info)
        elif info["typ"] == "FAT":
            self.dialog.add_fat(info)
        elif info["typ"] == "FDT":
            self.dialog.add_fdt(info)
        self.refresh_route_preview()

    def canvasMoveEvent(self, event):
        if not self.dialog._draw_active or not self.dialog._sequence:
            self._clear_hover()
            return
        hit = self._nearest(event.pos())
        if not hit:
            self._clear_hover()
            return
        _, _, info = hit
        route = self.dialog.prospective_route(info)
        if route:
            self._draw_hover(route)
        else:
            self._clear_hover()
        self.dialog._refresh_ui()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Backspace:
            self.dialog.undo_last()
            return
        if event.key() == Qt.Key_Escape:
            self.dialog._stop_tool()
            return
        super().keyPressEvent(event)

    def _to_canvas_points(self, route):
        points = route["points"]
        src = self.engine.edge_layer.crs()
        dst = self.canvas.mapSettings().destinationCrs()
        if src == dst:
            return points
        transform = QgsCoordinateTransform(src, dst, QgsProject.instance().transformContext())
        return [transform.transform(p) for p in points]

    def refresh_route_preview(self):
        self._clear_hover()
        if len(self.dialog._sequence) < 2:
            return
        points = []
        for first, second in zip(self.dialog._sequence[:-1], self.dialog._sequence[1:]):
            route = self.engine.route(first[0], first[1], second[0], second[1])
            if route:
                route_points = self._to_canvas_points(route)
                points.extend(route_points if not points else route_points[1:])
        if len(points) >= 2:
            band = QgsRubberBand(self.canvas, QgsWkbTypes.LineGeometry)
            band.setWidth(4)
            band.setColor(QtWidgets.QApplication.palette().highlight().color())
            band.setToGeometry(
                QgsGeometry.fromPolylineXY(points),
                self.canvas.mapSettings().destinationCrs(),
            )
            self._hover_band = band

    def _draw_hover(self, route):
        self._clear_hover()
        points = self._to_canvas_points(route)
        if len(points) < 2:
            return
        band = QgsRubberBand(self.canvas, QgsWkbTypes.LineGeometry)
        band.setWidth(3)
        band.setColor(QtWidgets.QApplication.palette().highlight().color())
        band.setToGeometry(
            QgsGeometry.fromPolylineXY(points),
            self.canvas.mapSettings().destinationCrs(),
        )
        self._hover_band = band

    def _clear_hover(self):
        if self._hover_band is not None:
            try:
                self.canvas.scene().removeItem(self._hover_band)
            except Exception:
                pass
            self._hover_band = None
