# -*- coding: utf-8 -*-
"""Project-scoped Link Design management layer.

The active ODN Project is the single source of truth for configuration.
Link Design state is keyed by the active .odn project, not by the QGIS .qgz.
Written links retain Distribution Cable feature ids so planned links can be
edited/deleted and already-written links can be safely replaced/deleted.
"""

import json
import os

from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import Qt, QSettings
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
)

from . import odn_project_context as context
from .link_design_v2 import (
    LinkDesignDialog as _BaseLinkDesignDialog,
    CompletedDesignDialog as _BaseCompletedDesignDialog,
    _fresh_payload,
    _project_state_key,
)


class LinkDesignDialog(_BaseLinkDesignDialog):
    """Link Design with strict project-scoped state and Link CRUD management."""

    def __init__(self, iface, parent=None):
        self._editing_written_index = None
        super().__init__(iface, parent)

    @staticmethod
    def _project_state_key():
        path = context.current_path()
        if path:
            path = os.path.normcase(os.path.abspath(path)).replace("\\", "/")
        else:
            path = "__NO_ACTIVE_ODN_PROJECT__"
        return f"ODNToolsPro/LinkDesign/state/odn/{path}"

    def _load_saved_state(self):
        # Never mix Link Design state between different ODN Projects.
        self._designs = []
        self._sequence = []
        self._editing_index = None
        self._editing_written_index = None
        try:
            raw = QSettings().value(self._project_state_key(), "")
            state = json.loads(str(raw)) if raw else None
        except Exception:
            state = None
        if isinstance(state, dict):
            designs = state.get("designs", [])
            draft = state.get("draft")
            self._designs = designs if isinstance(designs, list) else []
            if isinstance(draft, dict):
                self._restore_draft(draft)

    def _draft_payload(self):
        payload = super()._draft_payload()
        if payload is not None:
            payload["editing_written_index"] = self._editing_written_index
        return payload

    def _persist_state(self):
        state = {"designs": self._designs, "draft": self._draft_payload()}
        raw = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
        try:
            settings = QSettings()
            settings.setValue(self._project_state_key(), raw)
            settings.sync()
        except Exception:
            pass

    def _restore_draft(self, draft):
        super()._restore_draft(draft)
        try:
            value = draft.get("editing_written_index")
            self._editing_written_index = int(value) if value is not None else None
        except (TypeError, ValueError):
            self._editing_written_index = None

    def _clear_draft(self):
        super()._clear_draft()
        self._editing_written_index = None

    def _stop_tool(self):
        super()._stop_tool()
        self._editing_written_index = self._editing_written_index

    def _build_layer_features(self, layer, design):
        """Build DC features from the exact stored Pole Edge route geometry."""
        source_authid = design.get("source_crs")
        source_crs = self._engine.edge_layer.crs() if self._engine else None
        if source_authid:
            stored_crs = QgsCoordinateReferenceSystem(source_authid)
            if stored_crs.isValid():
                source_crs = stored_crs
        if source_crs is None or not source_crs.isValid():
            raise RuntimeError(f"{design.get('fdt')}/{design.get('link')} 缺少有效的路线 CRS。")

        target_crs = layer.crs()
        transform = None
        if source_crs != target_crs:
            transform = QgsCoordinateTransform(
                source_crs, target_crs, QgsProject.instance().transformContext()
            )

        features = []
        for segment in design.get("segments", []):
            raw_points = segment.get("points", [])
            try:
                points = [QgsPointXY(float(p[0]), float(p[1])) for p in raw_points]
            except Exception as exc:
                raise RuntimeError(
                    f"{design.get('fdt')}/{design.get('link')} 存在无效路线点：{exc}"
                )
            if len(points) < 2:
                raise RuntimeError(
                    f"{design.get('fdt')}/{design.get('link')} 存在无效线路段。"
                )
            if transform is not None:
                points = [transform.transform(p) for p in points]
            feature = QgsFeature(layer.fields())
            feature.setGeometry(QgsGeometry.fromPolylineXY(points))
            features.append(feature)
        if not features:
            raise RuntimeError(f"{design.get('fdt')}/{design.get('link')} 没有可写入的线路段。")
        return features

    def _write_design_features(self, layer, design):
        features = self._build_layer_features(layer, design)
        added = []
        try:
            for feature in features:
                if not layer.addFeature(feature):
                    raise RuntimeError(
                        f"无法写入 Distribution Cable：{design.get('fdt')}/{design.get('link')}"
                    )
                added.append(int(feature.id()))
        except Exception:
            for fid in added:
                try:
                    layer.deleteFeature(fid)
                except Exception:
                    pass
            raise
        return added

    def _delete_feature_ids(self, layer, fids):
        for fid in fids:
            fid = int(fid)
            feature = layer.getFeature(fid)
            if not feature.isValid():
                raise RuntimeError(f"Distribution Cable 要素 {fid} 不存在，无法安全删除。")
            if not layer.deleteFeature(fid):
                raise RuntimeError(f"无法删除 Distribution Cable 要素 {fid}。")

    def _replace_written_link(self, index, design):
        layer = context.project_layer(_fresh_payload(self), "Distribution Cable")
        if layer is None:
            QtWidgets.QMessageBox.warning(self, "修改 Link", "当前项目没有绑定 Distribution Cable 图层。")
            return False
        old = self._designs[index]
        old_fids = old.get("written_fids") or []
        if not old_fids:
            QtWidgets.QMessageBox.warning(
                self,
                "修改 Link",
                "该 Link 已写入图层，但旧版本没有保存对应的 Distribution Cable 要素 ID。\n\n"
                "为避免误删其他线路，当前版本不会猜测要素归属。请先处理该旧 Link。",
            )
            return False
        if not layer.isEditable() and not layer.startEditing():
            QtWidgets.QMessageBox.warning(self, "修改 Link", "无法进入 Distribution Cable 编辑状态。")
            return False

        try:
            new_fids = self._write_design_features(layer, design)
            try:
                self._delete_feature_ids(layer, old_fids)
            except Exception:
                for fid in new_fids:
                    try:
                        layer.deleteFeature(fid)
                    except Exception:
                        pass
                raise
        except Exception as exc:
            QtWidgets.QMessageBox.warning(
                self,
                "修改 Link",
                f"更新 {old.get('fdt')}/{old.get('link')} 失败，已回滚新线路：\n{exc}",
            )
            layer.triggerRepaint()
            return False

        design["written"] = True
        design["written_fids"] = new_fids
        self._designs[index] = design
        layer.triggerRepaint()
        return True

    def save_current_link(self):
        design = self._make_design()
        if not design:
            return False
        if self._editing_index is not None:
            index = self._editing_index
            if not (0 <= index < len(self._designs)):
                return False
            if self._editing_written_index is not None:
                if not self._replace_written_link(index, design):
                    return False
                text = f"已更新 {design['fdt']}/{design['link']}（已同步图层）"
            else:
                self._designs[index] = design
                text = f"已更新 {design['fdt']}/{design['link']}"
        else:
            same = next(
                (i for i, d in enumerate(self._designs)
                 if d.get("fdt") == design["fdt"] and d.get("link") == design["link"]),
                None,
            )
            if same is not None:
                if self._designs[same].get("written"):
                    QtWidgets.QMessageBox.warning(self, "已写入 Link", "当前 Link 已经写入图层，请从“已完成设计”选择修改。")
                    return False
                self._designs[same] = design
                text = f"已更新 {design['fdt']}/{design['link']}"
            else:
                self._designs.append(design)
                text = f"已保存 {design['fdt']}/{design['link']}"

        length = design["length"]
        self._clear_draft()
        self._persist_state()
        self._clear_saved_bands()
        self._refresh_ui()
        self.status.setText(f"状态：{text}（{length:.1f} m），请在地图上点击下一个 FDT 开始下一条链路。")
        return True

    def load_design_for_edit(self, index):
        try:
            index = int(index)
        except (TypeError, ValueError):
            return False
        if index < 0 or index >= len(self._designs):
            return False
        design = self._designs[index]
        seq = design.get("sequence_ids", [])
        labels = design.get("sequence", [])
        if not seq or len(seq) != len(labels):
            QtWidgets.QMessageBox.warning(self, "修改 Link", "该 Link 缺少有效的规划拓扑数据。")
            return False
        if design.get("written") and not design.get("written_fids"):
            QtWidgets.QMessageBox.warning(
                self,
                "修改 Link",
                "该 Link 属于旧版本已写入数据，没有保存对应的图层要素 ID，暂不能安全修改。",
            )
            return False
        try:
            self._sequence = [
                (str(item[0]), int(item[1]), str(labels[i]))
                for i, item in enumerate(seq)
            ]
        except Exception:
            return False
        self._editing_index = index
        self._editing_written_index = index if design.get("written") else None
        self._direction = design.get("direction") or "FDT_TO_FAT"
        self._current_fdt = str(design.get("fdt", ""))
        try:
            self._current_fdt_id = int(design.get("fdt_id"))
        except (TypeError, ValueError):
            self._current_fdt_id = self._sequence[0][1]
        self._current_link = str(design.get("link", ""))
        self._engine = self._prepare_engine()
        if self._engine is None:
            return False
        self.status.setText(
            f"状态：正在修改 {self._current_fdt}/{self._current_link}，"
            "调整 FAT 后点击“保存规划”。"
        )
        self._activate_map_tool()
        self._persist_state()
        self._refresh_ui()
        return True

    def delete_link(self, index):
        try:
            index = int(index)
        except (TypeError, ValueError):
            return False
        if index < 0 or index >= len(self._designs):
            return False
        design = self._designs[index]
        title = f"{design.get('fdt', '')}/{design.get('link', '')}"
        if design.get("written"):
            old_fids = design.get("written_fids") or []
            if not old_fids:
                QtWidgets.QMessageBox.warning(
                    self,
                    "删除 Link",
                    f"{title} 已写入图层，但没有保存对应的 Distribution Cable 要素 ID。\n\n"
                    "为避免误删其他线路，当前版本拒绝猜测归属。",
                )
                return False
            layer = context.project_layer(_fresh_payload(self), "Distribution Cable")
            if layer is None:
                QtWidgets.QMessageBox.warning(self, "删除 Link", "当前项目没有绑定 Distribution Cable 图层。")
                return False
            if not layer.isEditable() and not layer.startEditing():
                QtWidgets.QMessageBox.warning(self, "删除 Link", "无法进入 Distribution Cable 编辑状态。")
                return False
            answer = QtWidgets.QMessageBox.question(
                self,
                "删除 Link",
                f"确定删除 {title} 吗？\n\n这会同时删除它对应的 Distribution Cable 图形。",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No,
            )
            if answer != QtWidgets.QMessageBox.Yes:
                return False
            try:
                self._delete_feature_ids(layer, old_fids)
            except Exception as exc:
                QtWidgets.QMessageBox.warning(self, "删除 Link", f"删除失败，规划记录未删除：\n{exc}")
                layer.triggerRepaint()
                return False
            layer.triggerRepaint()

        else:
            answer = QtWidgets.QMessageBox.question(
                self,
                "删除 Link",
                f"确定删除 {title} 吗？\n\n该 Link 尚未写入图层。",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No,
            )
            if answer != QtWidgets.QMessageBox.Yes:
                return False

        del self._designs[index]
        if self._editing_index == index:
            self._clear_draft()
        elif self._editing_index is not None and self._editing_index > index:
            self._editing_index -= 1
        self._persist_state()
        self._clear_saved_bands()
        self._refresh_ui()
        self.status.setText(f"状态：已删除 {title}。")
        return True

    def open_completed_designs(self):
        dlg = CompletedDesignDialog(self)
        dlg.exec_()
        self._refresh_ui()

    def write_planned_links(self):
        """Write all not-yet-written plans and persist their DC feature ids."""
        layer = context.project_layer(_fresh_payload(self), "Distribution Cable")
        if layer is None:
            QtWidgets.QMessageBox.warning(self, "写入图层", "当前项目没有绑定 Distribution Cable 图层。")
            return False
        pending = [(i, d) for i, d in enumerate(self._designs) if not d.get("written")]
        if not pending:
            QtWidgets.QMessageBox.information(self, "写入图层", "没有待写入的已规划 Link。")
            return True
        if not layer.isEditable() and not layer.startEditing():
            QtWidgets.QMessageBox.warning(self, "写入图层", f"无法进入 Distribution Cable 编辑状态：{layer.name()}")
            return False

        added_map = {}
        try:
            for index, design in pending:
                added_map[index] = self._write_design_features(layer, design)
            for index, fids in added_map.items():
                self._designs[index]["written"] = True
                self._designs[index]["written_fids"] = fids
            layer.triggerRepaint()
            self._persist_state()
        except Exception as exc:
            for fids in added_map.values():
                for fid in fids:
                    try:
                        layer.deleteFeature(fid)
                    except Exception:
                        pass
            layer.triggerRepaint()
            QtWidgets.QMessageBox.warning(self, "写入图层", f"写入失败，已回滚本次写入：\n{exc}")
            return False

        self._refresh_ui()
        QtWidgets.QMessageBox.information(
            self,
            "写入图层",
            f"已将 {len(pending)} 条 Link 的路线写入 Distribution Cable。\n\n"
            "写入使用的是已保存的 Pole Edge 路线，并按当前目标图层 CRS 转换。"
        )
        return True


class CompletedDesignDialog(_BaseCompletedDesignDialog):
    """Completed Link browser with modify/delete/write actions."""

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
        self.tree.setHeaderLabels(["FDT / Link", "距离", "状态"])
        self.tree.setColumnWidth(0, 225)
        self.tree.setColumnWidth(1, 85)
        self.tree.setAlternatingRowColors(True)
        self.tree.itemClicked.connect(self._on_clicked)
        self.tree.itemDoubleClicked.connect(self._on_double_clicked)
        self.tree.currentItemChanged.connect(self._on_current_changed)
        root.addWidget(self.tree, 1)

        self.info = QtWidgets.QLabel("点击 FDT 查看全部 Link；点击 Link 查看路线。")
        self.info.setWordWrap(True)
        self.info.setStyleSheet("color:#666;")
        root.addWidget(self.info)

        row1 = QtWidgets.QHBoxLayout()
        self.modify_btn = QtWidgets.QPushButton("修改选中 Link")
        self.delete_btn = QtWidgets.QPushButton("删除选中 Link")
        row1.addWidget(self.modify_btn)
        row1.addWidget(self.delete_btn)
        root.addLayout(row1)

        row2 = QtWidgets.QHBoxLayout()
        self.write_btn = QtWidgets.QPushButton("确定并写入图层")
        close_btn = QtWidgets.QPushButton("关闭")
        row2.addWidget(self.write_btn)
        row2.addWidget(close_btn)
        root.addLayout(row2)

        self.modify_btn.clicked.connect(self._modify_selected)
        self.delete_btn.clicked.connect(self._delete_selected)
        self.write_btn.clicked.connect(self._write_all)
        close_btn.clicked.connect(self.accept)
        self.modify_btn.setEnabled(False)
        self.delete_btn.setEnabled(False)

    def _refresh_tree(self):
        self.tree.clear()
        grouped = {}
        for index, design in enumerate(self.main_dialog._designs):
            grouped.setdefault(str(design.get("fdt", "未知 FDT")), []).append((index, design))
        for fdt in sorted(grouped):
            root = QtWidgets.QTreeWidgetItem([fdt, "", ""])
            root.setData(0, Qt.UserRole, ("fdt", fdt))
            self.tree.addTopLevelItem(root)
            for index, design in sorted(grouped[fdt], key=lambda x: str(x[1].get("link", ""))):
                status = "已写入" if design.get("written") else "已规划"
                distance = f"{float(design.get('length', 0.0)):.1f}m"
                child = QtWidgets.QTreeWidgetItem([str(design.get("link", "L?")), distance, status])
                child.setData(0, Qt.UserRole, ("link", index))
                child.setToolTip(0, " - ".join(design.get("sequence", [])))
                root.addChild(child)
        self._on_current_changed(self.tree.currentItem(), None)

    def _selected_index(self):
        current = self.tree.currentItem()
        target = current.data(0, Qt.UserRole) if current else self._last_target
        if target and target[0] == "link":
            return int(target[1])
        return None

    def _on_current_changed(self, current, previous):
        target = current.data(0, Qt.UserRole) if current else None
        enabled = bool(target and target[0] == "link")
        self.modify_btn.setEnabled(enabled)
        self.delete_btn.setEnabled(enabled)

    def _modify_selected(self):
        index = self._selected_index()
        if index is None:
            QtWidgets.QMessageBox.information(self, "修改 Link", "请先选择一个 Link。")
            return
        if self.main_dialog.load_design_for_edit(index):
            self.accept()

    def _delete_selected(self):
        index = self._selected_index()
        if index is None:
            QtWidgets.QMessageBox.information(self, "删除 Link", "请先选择一个 Link。")
            return
        if self.main_dialog.delete_link(index):
            self._refresh_tree()

    def _on_double_clicked(self, item, column):
        target = item.data(0, Qt.UserRole)
        if target and target[0] == "link":
            self._modify_selected()

    def _write_all(self):
        if self.main_dialog.write_planned_links():
            self._refresh_tree()
