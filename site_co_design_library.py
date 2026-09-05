# -*- coding: utf-8 -*-
"""
site_co_design_library.py

Pure-Python implementation of naming algorithms. Only connection_point_naming_run
is replaced with a robust, spatial-index based algorithm per project spec.
"""
import math
import os
import re
import tempfile
from functools import cmp_to_key
from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import QVariant
from qgis.core import (
    QgsProject,
    QgsVectorLayer,
    QgsGeometry,
    QgsPointXY,
    QgsField,
    QgsSpatialIndex,
    QgsFeature,
    QgsCoordinateTransform,
    QgsWkbTypes,
    QgsVectorFileWriter,
    QgsMapLayer,
)


def dialog_open(dialog):
    try:
        return dialog.exec_()
    except Exception:
        try:
            dialog.show()
            return None
        except Exception:
            return None


def show_log_dialog(title: str, text: str):
    dialog = QtWidgets.QDialog()
    dialog.setWindowTitle(title)
    layout = QtWidgets.QVBoxLayout(dialog)
    text_edit = QtWidgets.QPlainTextEdit()
    text_edit.setReadOnly(True)
    text_edit.setPlainText(text)
    text_edit.setMinimumSize(520, 360)
    layout.addWidget(text_edit)
    buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok)
    buttons.accepted.connect(dialog.accept)
    layout.addWidget(buttons)
    dialog.exec_()


def populate_layer_controls(line_combo: QtWidgets.QComboBox, point_list: QtWidgets.QListWidget):
    """
    Populate UI controls with only Line vector layers (combo) and Point vector layers (multi-list).
    Each point-list item stores the layer id in Qt.UserRole for robust retrieval.
    """
    # local import of QtCore to avoid changing module-level imports
    from qgis.PyQt import QtCore
    line_combo.clear()
    point_list.clear()

    layers = QgsProject.instance().mapLayers().values()
    for layer in layers:
        # Only vector layers
        try:
            if layer.type() != QgsMapLayer.VectorLayer:
                continue
        except Exception:
            continue

        # geometry type check
        try:
            geom_type = QgsWkbTypes.geometryType(layer.wkbType())
        except Exception:
            continue

        if geom_type == QgsWkbTypes.LineGeometry:
            # Add to line combo: show layer.name(), store layer.id() in itemData
            line_combo.addItem(layer.name(), layer.id())
        elif geom_type == QgsWkbTypes.PointGeometry:
            # Add to point list as checkable item
            item = QtWidgets.QListWidgetItem(layer.name())
            item.setFlags(item.flags() | QtCore.Qt.ItemIsUserCheckable)
            item.setCheckState(QtCore.Qt.Unchecked)
            # store layer id for retrieval
            item.setData(QtCore.Qt.UserRole, layer.id())
            point_list.addItem(item)
    # If no line layers found, optionally disable controls / show message
    if line_combo.count() == 0:
        line_combo.addItem('(No line layers available)', None)


def _num_to_letter(n: int) -> str:
    if n <= 0:
        return ''
    result = ''
    while n > 0:
        n, rem = divmod(n - 1, 26)
        result = chr(ord('A') + rem) + result
    return result


def _ensure_field(layer: QgsVectorLayer, field_name: str):
    if not field_name:
        return None
    if field_name in [f.name() for f in layer.fields()]:
        return layer.fields().indexOf(field_name)
    layer.startEditing()
    layer.dataProvider().addAttributes([QgsField(field_name, QVariant.String)])
    layer.updateFields()
    return layer.fields().indexOf(field_name)


def _find_owner_field(layer: QgsVectorLayer, params: dict):
    requested = params.get('fat_owner_field_name')
    candidates = []
    if requested:
        candidates.append(requested)
    candidates.extend(['fat_owner', 'owner', 'cable_id', 'line_id', 'CableID', 'LineID', 'Owner'])
    available = [f.name() for f in layer.fields()]
    for field_name in candidates:
        if field_name and field_name in available:
            return field_name
    return None


def _resolve_owner_value(value, line_ids, line_name_map):
    if value is None:
        return None
    try:
        if isinstance(value, int):
            return value if value in line_ids else None
        if isinstance(value, float) and value.is_integer():
            int_value = int(value)
            return int_value if int_value in line_ids else None
    except Exception:
        pass
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        line_id = int(text)
        if line_id in line_ids:
            return line_id
    return line_name_map.get(text)


def _build_fat_owner_index(fat_feats, line_feats, owner_field_name, log):
    line_ids = {feat.id() for feat in line_feats}
    line_name_map = {}
    for feat in line_feats:
        if 'Name' in [f.name() for f in feat.fields()]:
            name = feat['Name']
            if name is not None and str(name).strip() != '':
                line_name_map[str(name)] = feat.id()
        line_name_map[str(feat.id())] = feat.id()

    fat_owner_index = {}
    fat_owner_map = {}
    skipped = 0
    for fat in fat_feats:
        owner_value = fat.attribute(owner_field_name)
        line_id = _resolve_owner_value(owner_value, line_ids, line_name_map)
        if line_id is None:
            skipped += 1
            log.append(f"WARNING: FAT {fat.id()} owner field '{owner_field_name}' value '{owner_value}' could not be mapped to any cable")
            continue
        fat_owner_index.setdefault(line_id, []).append(fat)
        fat_owner_map[fat.id()] = line_id
    return fat_owner_index, fat_owner_map, skipped


def _split_cable_geometry(line_geom, split_points):
    try:
        result = line_geom.splitGeometry(split_points, False)
    except Exception:
        return []
    geoms = _extract_split_geometries(result)
    return _flatten_geometry_list(geoms)


def _extract_split_geometries(result):
    """
    Normalize various possible return types from QgsGeometry.splitGeometry()
    into a list of QgsGeometry objects.

    This function recursively walks containers (tuples/lists) and extracts any
    QgsGeometry instances or objects exposing .geometry() / .geometries().
    It correctly handles QGIS 3.40's (result_code, geometry_list) tuple.
    """
    geoms = []

    def _collect(obj):
        # direct geometry
        try:
            if isinstance(obj, QgsGeometry):
                geoms.append(obj)
                return
        except Exception:
            pass
        # object with geometries()
        try:
            if hasattr(obj, 'geometries'):
                try:
                    for g in obj.geometries():
                        _collect(g)
                    return
                except Exception:
                    pass
        except Exception:
            pass
        # object with geometry()
        try:
            if hasattr(obj, 'geometry'):
                try:
                    g = obj.geometry()
                    _collect(g)
                    return
                except Exception:
                    pass
        except Exception:
            pass
        # iterable (list/tuple)
        try:
            if isinstance(obj, (list, tuple)):
                for item in obj:
                    _collect(item)
                return
        except Exception:
            pass
        # nothing matched
        return

    try:
        _collect(result)
    except Exception:
        return []
    return geoms


def _flatten_geometry_list(geoms):
    out = []
    for geom in geoms:
        if geom is None:
            continue
        if not isinstance(geom, QgsGeometry):
            try:
                geom = QgsGeometry(geom)
            except Exception:
                continue
        try:
            if geom.isEmpty():
                continue
        except Exception:
            continue
        wkb = geom.wkbType()
        if QgsWkbTypes.geometryType(wkb) == QgsWkbTypes.LineGeometry and geom.isMultipart():
            try:
                for part in geom.asMultiPolyline():
                    if not part:
                        continue
                    try:
                        part_geom = QgsGeometry.fromPolylineXY([QgsPointXY(p) for p in part])
                        if not part_geom.isEmpty():
                            out.append(part_geom)
                    except Exception:
                        continue
                continue
            except Exception:
                pass
        out.append(geom)
    return out


def _get_split_points(line_geom, fat_feats, transform):
    full_line_geom = QgsGeometry(line_geom)
    positions = []
    for fat in fat_feats:
        fat_geom = fat.geometry()
        if fat_geom is None or fat_geom.isEmpty():
            continue
        try:
            pg = QgsGeometry(fat_geom)
            if transform is not None:
                pg.transform(transform)
            pt = _cns_point_xy(pg)
            if pt is None:
                continue
            qpt = QgsGeometry.fromPointXY(pt)
            pos = full_line_geom.lineLocatePoint(qpt)
            if pos is None:
                continue
            positions.append((pos, QgsPointXY(pt)))
        except Exception:
            continue
    positions.sort(key=lambda x: x[0])
    split_points = []
    last_pos = None
    for pos, pt in positions:
        if last_pos is not None and abs(pos - last_pos) < 1e-9:
            continue
        split_points.append(pt)
        last_pos = pos
    return split_points


def _build_segment_layer(line_layer, layer_name='Cable Segment'):
    geom_type = QgsWkbTypes.displayString(line_layer.wkbType())
    segment_layer = QgsVectorLayer(f"{geom_type}?crs={line_layer.crs().authid()}", layer_name, 'memory')
    provider = segment_layer.dataProvider()
    provider.addAttributes(line_layer.fields())
    segment_layer.updateFields()
    return segment_layer


def _split_geometry_iterative(line_geom, split_points, log=None):
    """
    Iteratively split a line geometry by a list of QgsPointXY split_points.

    Important: QgsGeometry.splitGeometry() may modify the geometry object in-place
    (making it one of the resulting parts) and return only the newly created parts.
    Therefore after each split call, the algorithm will include the (possibly
    modified) original geometry along with the returned pieces as the replacement
    set for that original geometry.

    When 'log' is provided (a list), append per-round debug lines in the form:
      "Round N: Segments Before -> Segments After (splits=S)"
    """
    geoms = [QgsGeometry(line_geom)]
    round_idx = 0
    for pt in split_points:
        round_idx += 1
        before_count = len(geoms)
        splits_this_round = 0
        next_geoms = []
        for geom in geoms:
            try:
                if geom is None or geom.isEmpty():
                    continue
            except Exception:
                try:
                    geom = QgsGeometry(geom)
                except Exception:
                    continue

            # Save original geometry BEFORE split (per user's diagnosis)
            try:
                original_geom = QgsGeometry(geom)
            except Exception:
                original_geom = None

            # attempt split (this may mutate 'geom' in-place)
            result = None
            try:
                result = geom.splitGeometry([pt], False)
            except Exception:
                try:
                    result = geom.splitGeometry([QgsGeometry.fromPointXY(pt)], False)
                except Exception:
                    # if split fails, keep original geometry
                    next_geoms.append(geom)
                    continue

            # extract returned pieces
            pieces = _extract_split_geometries(result)
            pieces = _flatten_geometry_list(pieces)

            # After split, 'geom' is already modified into one of the parts.
            try:
                mutated_geom = QgsGeometry(geom)
            except Exception:
                mutated_geom = None

            # Combine mutated original + returned pieces
            combined = []
            if mutated_geom is not None:
                try:
                    if not mutated_geom.isEmpty():
                        combined.append(mutated_geom)
                except Exception:
                    pass

            for p in pieces:
                try:
                    is_dup = False
                    if mutated_geom is not None:
                        try:
                            # prefer geometry-level equality
                            if hasattr(mutated_geom, 'equals') and hasattr(p, 'equals'):
                                try:
                                    if mutated_geom.equals(p):
                                        is_dup = True
                                except Exception:
                                    pass
                        except Exception:
                            pass
                    if not is_dup:
                        combined.append(p)
                except Exception:
                    combined.append(p)

            # Determine split success by examining the raw result value from splitGeometry,
            # rather than by combined length alone.
            split_success = False
            split_reason = 'unknown'
            try:
                if isinstance(result, (list, tuple)) and len(result) > 0:
                    # QGIS 3.40: (result_code, geometry_list)
                    first = result[0]
                    try:
                        fname = str(getattr(first, 'name', first)).lower()
                    except Exception:
                        fname = str(first).lower()
                    if 'success' in fname or 'ok' in fname:
                        split_success = True
                        split_reason = f'flag:{fname}'
                    elif isinstance(first, int) and first == 0:
                        split_success = True
                        split_reason = 'int:0'
                    elif len(result) > 1 and result[1] is not None:
                        # result[1] should be geometry list in QGIS 3.40
                        split_success = True
                        split_reason = 'len>1_with_geoms'
                    else:
                        split_success = False
                        split_reason = f'flag_unknown:{fname}'
                else:
                    try:
                        s = str(result).lower()
                    except Exception:
                        s = ''
                    if 'success' in s or 'ok' in s:
                        split_success = True
                        split_reason = f'str:{s}'
                    elif isinstance(result, QgsGeometry):
                        split_success = True
                        split_reason = 'QgsGeometry'
                    else:
                        split_success = False
                        split_reason = 'single_unknown'
            except Exception as e:
                split_success = False
                split_reason = f'exception:{e}'

            # record debug details immediately after split
            try:
                extracted_count = len(pieces) if pieces is not None else 0
            except Exception:
                extracted_count = 0
            try:
                combined_count = len(combined)
            except Exception:
                combined_count = 0
            if log is not None:
                try:
                    log.append(f"Split attempt: result={split_reason}, extracted={extracted_count}, combined={combined_count}")
                except Exception:
                    pass

            if split_success:
                splits_this_round += 1

            if combined:
                next_geoms.extend(combined)
            else:
                # fallback: keep original geometry
                # use the mutated geom if available else the original
                if mutated_geom is not None:
                    next_geoms.append(mutated_geom)
                elif original_geom is not None:
                    next_geoms.append(original_geom)
                else:
                    next_geoms.append(geom)
        # If no splits happened in this round, stop further rounds (optimization)
        geoms = next_geoms
        if splits_this_round == 0:
            break
    return geoms




def _save_segment_layer(segment_layer, output_path):
    if not output_path:
        return segment_layer
    options = QgsVectorFileWriter.SaveVectorOptions()
    options.fileEncoding = 'UTF-8'
    options.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteFile
    suffix = output_path.split('.')[-1].lower()
    if suffix == 'gpkg':
        options.driverName = 'GPKG'
    elif suffix == 'shp':
        options.driverName = 'ESRI Shapefile'
    elif suffix == 'geojson':
        options.driverName = 'GeoJSON'
    else:
        options.driverName = 'GPKG'
    error = QgsVectorFileWriter.writeAsVectorFormatV2(
        segment_layer,
        output_path,
        QgsProject.instance().transformContext(),
        options,
    )
    if isinstance(error, tuple):
        error = error[0]
    if error != QgsVectorFileWriter.NoError:
        return None
    output_layer = QgsVectorLayer(output_path, segment_layer.name(), 'ogr')
    if output_layer.isValid():
        return output_layer
    return None


def _default_segment_output_path():
    project_home = QgsProject.instance().homePath()
    if project_home:
        output_dir = project_home
    else:
        output_dir = tempfile.gettempdir()
    return os.path.join(output_dir, 'Cable Segment.gpkg')


def _copy_segment_feature(source_feat, target_fields, geometry):
    feat = QgsFeature(target_fields)
    feat.setAttributes(source_feat.attributes())
    feat.setGeometry(geometry)
    return feat


def _format_geometry(geom):
    try:
        return geom.asWkt()
    except Exception:
        return str(geom)


def _cns_distance(p1, p2):
    try:
        return QgsPointXY(p1).distance(QgsPointXY(p2))
    except Exception:
        return float('inf')


def _cns_point_key(pt):
    if pt is None:
        return None
    return (round(pt.x(), 6), round(pt.y(), 6))

def _cns_point_xy(geom):
    if geom is None or geom.isEmpty():
        return None
    try:
        wkb_type = geom.wkbType()
        geom_type = geom.type()
        if geom_type == QgsWkbTypes.PointGeometry:
            if geom.isMultipart() or QgsWkbTypes.isMultiPointType(wkb_type):
                points = geom.asMultiPoint()
                if points:
                    return QgsPointXY(points[0])
            pt = geom.asPoint()
            return QgsPointXY(pt)
        if geom_type == QgsWkbTypes.PolygonGeometry:
            centroid = geom.centroid()
            if centroid is not None and not centroid.isEmpty():
                try:
                    pt = centroid.asPoint()
                    return QgsPointXY(pt)
                except Exception:
                    pass
        if geom_type == QgsWkbTypes.LineGeometry:
            if geom.isMultipart():
                parts = geom.asMultiPolyline()
                if parts and parts[0]:
                    return QgsPointXY(parts[0][0])
            else:
                pts = geom.asPolyline()
                if pts:
                    return QgsPointXY(pts[0])
    except Exception:
        return None
    return None


def _cns_is_geographic(crs):
    try:
        return crs.isGeographic()
    except Exception:
        return False


def _cns_connection_tolerance(crs):
    return 1e-5 if _cns_is_geographic(crs) else 0.5


def _cns_project_point_on_line(line_geom, pt, tolerance):
    try:
        qpt = QgsGeometry.fromPointXY(pt)
        pos = line_geom.lineLocatePoint(qpt)
        proj = line_geom.interpolate(pos)
        if proj is None or proj.isEmpty():
            return None, None
        dist = proj.distance(qpt)
        if dist <= tolerance * 10:
            return pos, dist
    except Exception:
        pass
    return None, None


def _cns_get_node_by_point(pt, nodes, point_node_map, tolerance, allowed_types=None):
    key = _cns_point_key(pt)
    if key is not None:
        for node_id in point_node_map.get(key, []):
            if allowed_types is not None and nodes.get(node_id, {}).get('type') not in allowed_types:
                continue
            if _cns_distance(pt, nodes[node_id]['geom']) <= tolerance:
                return node_id
    for node_id, node in nodes.items():
        if allowed_types is not None and node.get('type') not in allowed_types:
            continue
        if _cns_distance(pt, node['geom']) <= tolerance:
            return node_id
    return None


def _cns_parse_fat_name(name):
    if name is None:
        return None
    try:
        text = str(name).strip()
    except Exception:
        return None
    if not text:
        return None
    m = re.match(r'^([A-Za-z]+)(\d+)L(\d+)S(\d+)$', text, re.IGNORECASE)
    if m:
        return (m.group(1).upper(), int(m.group(2)), int(m.group(3)), int(m.group(4)))
    return None


def _cns_compare_fat_name(name1, name2):
    key1 = _cns_parse_fat_name(name1)
    key2 = _cns_parse_fat_name(name2)
    if key1 is not None and key2 is not None:
        if key1 < key2:
            return -1
        if key1 > key2:
            return 1
        return 0
    if key1 is not None:
        return -1
    if key2 is not None:
        return 1
    if str(name1) < str(name2):
        return -1
    if str(name1) > str(name2):
        return 1
    return 0


def _cns_sort_fat_names(names):
    return sorted(names, key=cmp_to_key(_cns_compare_fat_name))


def _cns_extract_line_endpoints(geom):
    if geom is None or geom.isEmpty():
        return None, None
    if geom.isMultipart():
        parts = geom.asMultiPolyline()
        if not parts or not parts[0] or not parts[-1]:
            return None, None
        return QgsPointXY(parts[0][0]), QgsPointXY(parts[-1][-1])
    pts = geom.asPolyline()
    if not pts or len(pts) < 2:
        return None, None
    return QgsPointXY(pts[0]), QgsPointXY(pts[-1])


def _cns_get_node_label(node):
    if node is None:
        return None
    return node.get('name') or node.get('node_id')


def _cns_generate_cable_names(line_feats, nodes, connection_tol, point_node_map, log):
    cable_names = {}
    for lf in line_feats:
        geom = lf.geometry()
        if geom is None or geom.isEmpty():
            continue
        start_pt, end_pt = _cns_extract_line_endpoints(geom)
        if start_pt is None or end_pt is None:
            log.append(f"Cable {lf.id()} geometry invalid for endpoint extraction")
            continue
        start_node = _cns_get_node_by_point(start_pt, nodes, point_node_map, connection_tol, allowed_types={'FDT', 'FAT'})
        end_node = _cns_get_node_by_point(end_pt, nodes, point_node_map, connection_tol, allowed_types={'FDT', 'FAT'})
        if start_node is None or end_node is None:
            log.append(f"Cable {lf.id()} could not resolve both endpoints: start={start_node}, end={end_node}")
            continue
        if start_node == end_node:
            log.append(f"Cable {lf.id()} endpoints resolved to the same node {start_node}")
            continue
        start_node_obj = nodes.get(start_node)
        end_node_obj = nodes.get(end_node)
        if start_node_obj is None or end_node_obj is None:
            log.append(f"Cable {lf.id()} endpoint nodes missing from node table")
            continue
        start_type = start_node_obj.get('type')
        end_type = end_node_obj.get('type')
        start_name = _cns_get_node_label(start_node_obj)
        end_name = _cns_get_node_label(end_node_obj)
        if start_name is None or end_name is None:
            log.append(f"Cable {lf.id()} endpoint name missing")
            continue
        if start_type == 'FDT' and end_type == 'FAT':
            cable_name = f"{start_name}-{end_name}"
        elif start_type == 'FAT' and end_type == 'FDT':
            cable_name = f"{end_name}-{start_name}"
        elif start_type == 'FAT' and end_type == 'FAT':
            names = _cns_sort_fat_names([start_name, end_name])
            cable_name = f"{names[0]}-{names[1]}"
        else:
            cable_name = f"{start_name}-{end_name}"
        cable_names[lf.id()] = cable_name
    return cable_names


def _cns_build_nodes(feats, node_type, transform=None, field_name=None, id_prefix=None, log=None):
    nodes = {}
    input_count = len(feats)
    output_count = 0
    if log is not None and node_type == 'FAT':
        log.append('=========================')
        log.append('FAT Node Build')
        log.append('=========================')
        log.append(f'Input FAT Feature Count : {input_count}')
    input_sample_count = 0
    for feat in feats:
        geom = feat.geometry()
        if geom is None:
            if log is not None and node_type == 'FAT':
                log.append(f'FAT Feature ID {feat.id()}:')
                log.append('- geometry is None')
            continue
        # Diagnostic sample for first few input FAT geometries
        if log is not None and node_type == 'FAT' and input_sample_count < 10:
            try:
                wkb = geom.wkbType()
                wkb_name = QgsWkbTypes.displayString(wkb)
            except Exception:
                wkb_name = 'unknown'
            try:
                multipart = geom.isMultipart()
            except Exception:
                multipart = 'unknown'
            try:
                wkt = geom.asWkt()[:200]
            except Exception:
                wkt = 'wkt_failed'
            log.append(f'FAT Feature ID {feat.id()} sample: wkb={wkb_name}, multipart={multipart}, wkt={wkt}')
            input_sample_count += 1
        if geom.isEmpty():
            if log is not None and node_type == 'FAT':
                log.append(f'FAT Feature ID {feat.id()}:')
                log.append('- geometry empty')
            continue
        try:
            geom_to_line = QgsGeometry(geom)
            if transform is not None:
                geom_to_line.transform(transform)
        except Exception:
            geom_to_line = geom
        pt = _cns_point_xy(geom_to_line)
        # fallback: try centroid if point extraction failed
        if pt is None:
            try:
                cent = geom_to_line.centroid()
                if cent is not None and not cent.isEmpty():
                    try:
                        p = cent.asPoint()
                        pt = QgsPointXY(p)
                    except Exception:
                        pt = None
            except Exception:
                pt = None
        if pt is None:
            if log is not None and node_type == 'FAT':
                log.append(f'FAT Feature ID {feat.id()}:')
                log.append('- point conversion failed')
            continue
        if node_type == 'FDT':
            try:
                name = feat[field_name] if field_name else None
            except Exception:
                name = None
            if name is None or str(name).strip() == '':
                name = str(feat.id())
        else:
            name = None
            try:
                field_names = [f.name() for f in feat.fields()]
                if 'Name' in field_names:
                    candidate = feat['Name']
                    if candidate is not None and str(candidate).strip() != '':
                        name = str(candidate)
            except Exception:
                name = None
            if name is None:
                try:
                    for f in feat.fields():
                        if f.typeName() == 'String':
                            candidate = feat.attribute(f.name())
                            if candidate is not None and str(candidate).strip() != '':
                                name = str(candidate)
                                break
                except Exception:
                    name = None
            if name is None or str(name).strip() == '':
                name = str(feat.id())
        prefix = f"{id_prefix}_" if id_prefix else ''
        node_id = f"{prefix}{node_type}_{feat.id()}"
        nodes[node_id] = {
            'node_id': node_id,
            'type': node_type,
            'feat_id': feat.id(),
            'geom': pt,
            'name': str(name),
            'feature': feat,
        }
        output_count += 1
    if log is not None and node_type == 'FAT':
        log.append(f'Output FAT Node Count : {output_count}')
    return nodes


def _cns_angle(origin, target):
    try:
        vx = target.x() - origin.x()
        vy = target.y() - origin.y()
        return math.degrees(math.atan2(vy, vx)) % 360.0
    except Exception:
        return 0.0


def _cns_get_other_node(edge, node_id):
    if edge.get('node_a') == node_id:
        return edge.get('node_b')
    if edge.get('node_b') == node_id:
        return edge.get('node_a')
    return None


def _cns_node_label(node):
    if node is None:
        return 'UNKNOWN'
    if node['type'] == 'FDT':
        return node.get('name', node['node_id'])
    if node['type'] == 'FAT':
        return node.get('name', f"FAT[{node['feat_id']}]")
    return node.get('node_id')


def _cns_assign_fat_owner(fat_nodes, line_feats, connection_tol, log):
    fat_owner = {}
    fat_matches = {fat_id: set() for fat_id in fat_nodes}

    def _extract_line_endpoints(geom):
        if geom is None or geom.isEmpty():
            return None, None
        if geom.isMultipart():
            parts = geom.asMultiPolyline()
            if not parts or not parts[0] or not parts[-1]:
                return None, None
            return QgsPointXY(parts[0][0]), QgsPointXY(parts[-1][-1])
        pts = geom.asPolyline()
        if not pts or len(pts) < 2:
            return None, None
        return QgsPointXY(pts[0]), QgsPointXY(pts[-1])

    for lf in line_feats:
        geom = lf.geometry()
        if geom is None or geom.isEmpty():
            continue
        start_pt, end_pt = _extract_line_endpoints(geom)
        if start_pt is None or end_pt is None:
            continue
        for fat_id, fat in fat_nodes.items():
            try:
                dist_start = _cns_distance(fat['geom'], start_pt)
                dist_end = _cns_distance(fat['geom'], end_pt)
            except Exception:
                continue
            if dist_start is None or dist_end is None:
                continue
            if dist_start <= connection_tol or dist_end <= connection_tol:
                fat_matches[fat_id].add(lf.id())

    for fat_id, matches in fat_matches.items():
        fat = fat_nodes.get(fat_id)
        if not matches:
            if fat is not None:
                log.append(f"Topology error: FAT {fat['feat_id']} is not connected to any cable")
            continue
        if len(matches) > 1:
            if fat is not None:
                log.append(f"Topology error: FAT {fat['feat_id']} connected to multiple cables: {matches}")
            continue
        fat_owner[fat_id] = matches[0]

    return fat_owner


def _cns_get_cable_fat_mapping(fat_layer, line_layer, fdt_layer=None, params=None, log=None):
    """
    Produce a mapping from line feature id -> list of FAT feature objects.
    This centralizes the FAT->Cable owner logic so Connection Point Naming
    and Cable Split use the same source of truth.
    Returns a dict: { line_id: [QgsFeature, ...], ... }
    """
    proj = QgsProject.instance()
    # collect features
    line_feats = list(line_layer.getFeatures())
    fat_feats = list(fat_layer.getFeatures())
    # prepare transform from fat to line CRS
    try:
        tc = proj.transformContext()
        trans_fat_to_line = QgsCoordinateTransform(fat_layer.crs(), line_layer.crs(), tc)
    except Exception:
        trans_fat_to_line = None
    # build fat nodes in line CRS
    fat_nodes = _cns_build_nodes(fat_feats, 'FAT', transform=trans_fat_to_line, log=log)
    # compute tolerance and assign owners using the same function as Connection Point Naming
    connection_tol = _cns_connection_tolerance(line_layer.crs())
    fat_owner = _cns_assign_fat_owner(fat_nodes, line_feats, connection_tol, log or [])
    # diagnostic sample of fat_owner keys/types for debugging
    if log is not None:
        log.append('')
        log.append('========== FAT OWNER SAMPLE ==========')
        count = 0
        for k, v in fat_owner.items():
            try:
                log.append(f"{repr(k)} ({type(k).__name__}) -> {repr(v)}")
            except Exception:
                log.append(f"{repr(k)} -> {repr(v)}")
            count += 1
            if count >= 10:
                break
        log.append('======================================')
    # map back to real fat feature objects
    fat_feat_by_id = {feat.id(): feat for feat in fat_feats}
    mapping = {}
    for fat_node_id, line_id in fat_owner.items():
        try:
            fat_id = int(fat_node_id.split('_', 1)[1])
        except Exception:
            continue
        fat_feat = fat_feat_by_id.get(fat_id)
        if fat_feat is not None:
            mapping.setdefault(line_id, []).append(fat_feat)
    return mapping


def _cns_build_graph(fdt_nodes, fat_nodes, line_feats, connection_tol, fat_owner, log):
    nodes = {}
    nodes.update(fdt_nodes)
    nodes.update(fat_nodes)
    point_node_map = {}
    for node_id, node in nodes.items():
        key = _cns_point_key(node['geom'])
        if key is None:
            continue
        point_node_map.setdefault(key, []).append(node_id)

    adjacency = {}
    edge_count = 0

    def _add_edge(node_a, node_b, line_id, part_index, length):
        nonlocal edge_count
        if node_a is None or node_b is None or node_a == node_b:
            return
        edge_id = f"E_{line_id}_{part_index}_{edge_count}"
        edge = {
            'id': edge_id,
            'line_id': line_id,
            'part_index': part_index,
            'node_a': node_a,
            'node_b': node_b,
            'length': length,
        }
        edge_count += 1
        adjacency.setdefault(node_a, []).append(edge)
        adjacency.setdefault(node_b, []).append(edge)

    edge_set = set()
    for lf in line_feats:
        geom = lf.geometry()
        if geom is None or geom.isEmpty():
            continue
        line_id = lf.id()
        owned_fats = [fid for fid, lid in fat_owner.items() if lid == line_id]
        fat_count = len(owned_fats)
        log.append("Graph Build:")
        log.append(f"Cable {line_id}")
        log.append(f"Owner FAT count={fat_count}")

        if geom.isMultipart():
            parts = geom.asMultiPolyline()
            if not parts or not parts[0] or not parts[-1]:
                continue
            start_pt = QgsPointXY(parts[0][0])
            end_pt = QgsPointXY(parts[-1][-1])
            full_line_geom = QgsGeometry(geom)
        else:
            pts = geom.asPolyline()
            if not pts or len(pts) < 2:
                continue
            start_pt = QgsPointXY(pts[0])
            end_pt = QgsPointXY(pts[-1])
            full_line_geom = QgsGeometry(geom)

        line_len = full_line_geom.length()
        if line_len <= 0:
            continue

        start_node = _cns_get_node_by_point(start_pt, nodes, point_node_map, connection_tol, allowed_types={'FDT'})
        end_node = _cns_get_node_by_point(end_pt, nodes, point_node_map, connection_tol, allowed_types={'FDT'})

        node_positions = {}
        if start_node is not None:
            node_positions[start_node] = 0.0
        if end_node is not None and end_node != start_node:
            node_positions[end_node] = line_len

        def _resolve_fat_node(fat_id):
            fat = fat_nodes.get(fat_id)
            if fat is not None:
                return fat
            fat = fat_nodes.get(str(fat_id))
            if fat is not None:
                return fat
            try:
                fat = fat_nodes.get(int(fat_id))
                if fat is not None:
                    return fat
            except Exception:
                pass
            if isinstance(fat_id, str):
                if not fat_id.startswith('FAT_'):
                    fat = fat_nodes.get(f"FAT_{fat_id}")
                    if fat is not None:
                        return fat
            else:
                fat = fat_nodes.get(f"FAT_{fat_id}")
                if fat is not None:
                    return fat
            return None

        if fat_count and log is not None:
            sample = []
            for fid in owned_fats[:10]:
                sample.append({
                    'fat_id': fid,
                    'has_exact': fid in fat_nodes,
                    'has_str': str(fid) in fat_nodes,
                    'has_fat_prefix': f"FAT_{fid}" in fat_nodes,
                })
            log.append(f"owned_fats sample: {sample}")

        for fat_id in owned_fats:
            fat = _resolve_fat_node(fat_id)
            if fat is None:
                continue
            try:
                qpt = QgsGeometry.fromPointXY(fat['geom'])
                pos = full_line_geom.lineLocatePoint(qpt)
            except Exception:
                continue
            if pos is None:
                continue
            node_positions[fat['node_id']] = min(node_positions.get(fat['node_id'], pos), pos)

        graph_fat_count = sum(1 for nid in node_positions.keys() if nodes.get(nid, {}).get('type') == 'FAT')
        log.append(f"Graph FAT count={graph_fat_count}")
        if graph_fat_count > fat_count:
            log.append("ERROR: Graph加入了非Owner FAT")

        if len(node_positions) < 2:
            continue

        sorted_positions = sorted(node_positions.items(), key=lambda item: item[1])
        ordered_nodes = []
        last_node_id = None
        for node_id, pos in sorted_positions:
            if node_id == last_node_id:
                continue
            ordered_nodes.append((pos, node_id))
            last_node_id = node_id

        part_index = 0
        for i in range(len(ordered_nodes) - 1):
            node_a = ordered_nodes[i][1]
            node_b = ordered_nodes[i + 1][1]
            if node_a == node_b:
                continue
            edge_key = (node_a, node_b) if node_a < node_b else (node_b, node_a)
            if edge_key in edge_set:
                continue
            edge_set.add(edge_key)
            segment_length = ordered_nodes[i + 1][0] - ordered_nodes[i][0]
            _add_edge(node_a, node_b, line_id, part_index, segment_length)

    return nodes, adjacency


def _cns_validate_graph(nodes, adjacency, log):
    seen_edges = set()
    for node in nodes.values():
        node_id = node['node_id']
        degree = len(adjacency.get(node_id, []))
        if node['type'] == 'FDT' and degree == 0:
            log.append(f"Topology error: FDT {node.get('name')} has no direct cables")
        if node['type'] == 'FAT':
            if degree == 0:
                log.append(f"Topology error: FAT {node.get('feat_id')} is not connected to any cable")
            if degree > 2:
                log.append(f"Topology error: FAT {node.get('feat_id')} has {degree} connected cables")

    for node_id, edges in adjacency.items():
        for edge in edges:
            if edge['id'] in seen_edges:
                continue
            seen_edges.add(edge['id'])
            node_a = edge.get('node_a')
            node_b = edge.get('node_b')
            if not node_a or not node_b:
                continue
            if nodes.get(node_a, {}).get('type') == 'FDT' and nodes.get(node_b, {}).get('type') == 'FDT':
                log.append(
                    f"Topology error: Cable {edge.get('line_id')} part {edge.get('part_index')} directly connects FDT {nodes[node_a].get('name')} and FDT {nodes[node_b].get('name')}"
                )


def _cns_sort_direct_edges(fdt_node_id, nodes, adjacency, dir_enum):
    branch_edges = adjacency.get(fdt_node_id, [])
    branch_angles = []
    origin = nodes[fdt_node_id]['geom']
    for edge in branch_edges:
        neighbor_id = _cns_get_other_node(edge, fdt_node_id)
        neighbor = nodes.get(neighbor_id)
        angle = _cns_angle(origin, neighbor['geom']) if neighbor is not None else 0.0
        branch_angles.append((angle, edge))
    branch_angles.sort(key=lambda x: x[0], reverse=(dir_enum == 'clockwise'))
    return [edge for _, edge in branch_angles]


def _cns_walk_branch(fdt_node_id, first_edge, nodes, adjacency, log):
    branch_fats = []
    path = [fdt_node_id]
    visited_edges = {first_edge['id']}
    visited_nodes = {fdt_node_id}
    current_node = _cns_get_other_node(first_edge, fdt_node_id)
    if current_node is None:
        log.append(f"Topology error: branch from FDT {nodes[fdt_node_id].get('name')} has invalid first edge")
        return branch_fats, path, visited_edges

    while True:
        if current_node in visited_nodes:
            log.append(f"Topology error: cycle detected in branch from {nodes[fdt_node_id].get('name')}")
            break
        visited_nodes.add(current_node)
        path.append(current_node)
        node = nodes.get(current_node)
        if node is None:
            log.append(f"Topology error: branch reached unknown node {current_node}")
            break
        if node['type'] == 'FAT':
            branch_fats.append(current_node)
        next_edges = [e for e in adjacency.get(current_node, []) if e['id'] not in visited_edges]
        if not next_edges:
            break
        if len(next_edges) != 1:
            log.append(f"Topology error: node {_cns_point_key(node['geom'])} has {len(next_edges)} continuations")
            break
        next_edge = next_edges[0]
        visited_edges.add(next_edge['id'])
        current_node = _cns_get_other_node(next_edge, current_node)
        if current_node is None:
            log.append(f"Topology error: invalid continuation from node {node['node_id']}")
            break

    return branch_fats, path, visited_edges


def _cns_generate_cable_assignments(fdt_nodes, nodes, adjacency, log, dir_enum='clockwise'):
    cable_assignments = {}
    visited_edges = set()
    for fdt_node in fdt_nodes.values():
        fdt_id = fdt_node['node_id']
        direct_edges = [edge for edge in adjacency.get(fdt_id, []) if _cns_get_other_node(edge, fdt_id) is not None]
        if not direct_edges:
            continue
        ordered_edges = _cns_sort_direct_edges(fdt_id, nodes, adjacency, dir_enum)
        for edge in ordered_edges:
            if edge['id'] in visited_edges:
                continue
            branch_fats, branch_path, branch_edges = _cns_walk_branch(fdt_id, edge, nodes, adjacency, log)
            visited_edges |= branch_edges
            if len(branch_path) < 2:
                continue
            for i in range(len(branch_path) - 1):
                parent = branch_path[i]
                child = branch_path[i + 1]
                parent_name = nodes.get(parent, {}).get('name', str(parent))
                child_name = nodes.get(child, {}).get('name', str(child))
                cable_name = f"{parent_name}-{child_name}"
                connection_edge = None
                for e in adjacency.get(parent, []):
                    if _cns_get_other_node(e, parent) == child:
                        connection_edge = e
                        break
                if connection_edge is None:
                    log.append(f"Topology warning: no edge found between {parent_name} and {child_name}")
                    continue
                line_id = connection_edge.get('line_id')
                if line_id in cable_assignments and cable_assignments[line_id] != cable_name:
                    log.append(
                        f"Topology warning: conflicting cable names for line {line_id}: '{cable_assignments[line_id]}' vs '{cable_name}'"
                    )
                cable_assignments[line_id] = cable_name
    return cable_assignments


def _cns_generate_name(prefix, fdt_name, lidx, sidx, varL, varLSuf, varS, varSSuf):
    if varLSuf == 'letter':
        Lpart = f"{varL}{_num_to_letter(lidx)}"
    else:
        Lpart = f"{varL}{lidx}"
    if varSSuf == 'letter':
        Spart = f"{varS}{_num_to_letter(sidx)}"
    else:
        Spart = f"{varS}{sidx}"
    return f"{prefix}{fdt_name}{Lpart}{Spart}"


def connection_point_naming_run(params: dict, iface=None):
    proj = QgsProject.instance()
    line_layer_name = params.get('line_layer_name')
    conv_layer_name = params.get('conv_point_layer_name')
    conv_field = params.get('conv_point_layer_field_name')
    norm_layer_name = params.get('norm_point_layer_name')

    if not line_layer_name or not conv_layer_name or not norm_layer_name:
        QtWidgets.QMessageBox.critical(None, 'Site Co-Design', '缺少必要图层参数')
        return

    line_layers = proj.mapLayersByName(line_layer_name)
    conv_layers = proj.mapLayersByName(conv_layer_name)
    norm_layers = proj.mapLayersByName(norm_layer_name)
    if not line_layers or not conv_layers or not norm_layers:
        QtWidgets.QMessageBox.critical(None, 'Site Co-Design', '未找到指定图层')
        return

    line_layer = line_layers[0]
    conv_layer = conv_layers[0]
    norm_layer = norm_layers[0]

    if isinstance(norm_layer_name, list) and norm_layer_name:
        norm_layer_name = norm_layer_name[0]

    write_mode = params.get('data_write_mode', 'addAttr')
    add_field = params.get('add_attr_name', '')
    mod_field = params.get('modify_attr_name', '')
    target_field_name = mod_field if write_mode == 'modifyAttr' else add_field
    if not target_field_name:
        QtWidgets.QMessageBox.critical(None, 'Site Co-Design', '目标字段未指定')
        return
    if write_mode == 'addAttr':
        _ensure_field(norm_layer, target_field_name)
        _ensure_field(line_layer, target_field_name)
    field_idx = norm_layer.fields().indexOf(target_field_name)
    line_field_idx = line_layer.fields().indexOf(target_field_name)
    if field_idx < 0:
        QtWidgets.QMessageBox.critical(None, 'Site Co-Design', f'目标字段"{target_field_name}"不存在')
        return

    varL = params.get('variableL', 'L')
    varS = params.get('variableS', 'S')
    varLSuf = params.get('variableLSuf', 'num')
    varSSuf = params.get('variableSSuf', 'num')
    prefix = params.get('prefix', '') or ''
    dir_enum = params.get('point_name_directions_enum', 'clockwise')

    conv_feats = list(conv_layer.getFeatures())
    line_feats = list(line_layer.getFeatures())
    norm_feats = list(norm_layer.getFeatures())

    log = []
    log.append(f"FDT count: {len(conv_feats)}")
    log.append(f"Cable count: {len(line_feats)}")
    log.append(f"FAT count: {len(norm_feats)}")
    log.append('==================')

    try:
        tc = QgsProject.instance().transformContext()
        trans_conv_to_line = QgsCoordinateTransform(conv_layer.crs(), line_layer.crs(), tc)
    except Exception:
        trans_conv_to_line = None
    try:
        tc = QgsProject.instance().transformContext()
        trans_norm_to_line = QgsCoordinateTransform(norm_layer.crs(), line_layer.crs(), tc)
    except Exception:
        trans_norm_to_line = None

    connection_tol = _cns_connection_tolerance(line_layer.crs())

    fdt_nodes = _cns_build_nodes(conv_feats, 'FDT', transform=trans_conv_to_line, field_name=conv_field)
    fat_nodes = _cns_build_nodes(norm_feats, 'FAT', transform=trans_norm_to_line)

    fat_owner = _cns_assign_fat_owner(fat_nodes, line_feats, connection_tol, log)
    if len(fat_owner) != len(fat_nodes):
        log.append(f"Topology error: FAT owner assignment incomplete ({len(fat_owner)} of {len(fat_nodes)} FAT assigned)")
        summary = '\n'.join(log)
        show_log_dialog('Site Co-Design', 'FAT 所有者分配失败，拓扑构建中止。\n\n' + summary)
        return
    nodes, adjacency = _cns_build_graph(fdt_nodes, fat_nodes, line_feats, connection_tol, fat_owner, log)
    _cns_validate_graph(nodes, adjacency, log)

    cable_assignments = _cns_generate_cable_assignments(fdt_nodes, nodes, adjacency, log, dir_enum=dir_enum)
    log.append(f"Generated Cable Assignments: {len(cable_assignments)}")

    assignments = {}
    fat_assignments = {}
    connected_cable_ids = set()

    for fdt_node in fdt_nodes.values():
        fdt_id = fdt_node['node_id']
        direct_edges = [edge for edge in adjacency.get(fdt_id, []) if _cns_get_other_node(edge, fdt_id) is not None]
        if not direct_edges:
            log.append(f"FDT {fdt_node['name']} has no direct cables")
            continue

        ordered_edges = _cns_sort_direct_edges(fdt_id, nodes, adjacency, dir_enum)
        local_cable_ids = {edge['line_id'] for edge in ordered_edges if edge.get('line_id') is not None}
        connected_cable_ids.update(local_cable_ids)

        log.append(f"FDT {fdt_node['name']}")
        log.append(f"{fdt_node['name']}: Connected Cable Feature : {len(local_cable_ids)}")

        for lidx, edge in enumerate(ordered_edges, start=1):
            log.append(f"Cable Feature #{edge.get('line_id')} -> L{lidx}")
            branch_fats, branch_path, _ = _cns_walk_branch(fdt_id, edge, nodes, adjacency, log)
            path_labels = [_cns_node_label(nodes.get(node_id)) for node_id in branch_path]
            log.append(f"L{lidx}: {' -> '.join(path_labels)}")
            log.append(f"Total FAT: {len(branch_fats)}")
            for sidx, fat_node_id in enumerate(branch_fats, start=1):
                fat_node = nodes.get(fat_node_id)
                if fat_node is None:
                    continue
                branch_label = f"{fdt_node['name']}L{lidx}"
                if fat_node_id in fat_assignments:
                    previous_branch = fat_assignments[fat_node_id]
                    log.append(
                        f"Topology error: FAT {_cns_node_label(fat_node)} is reachable from multiple branches: previously {previous_branch}, now {branch_label}"
                    )
                    continue
                name = _cns_generate_name(prefix, fdt_node['name'], lidx, sidx, varL, varLSuf, varS, varSSuf)
                assignments[fat_node['feat_id']] = name
                fat_assignments[fat_node_id] = branch_label
        log.append(f"Generated Link Count : {len(ordered_edges)}")

    unassigned_fats = [nid for nid in fat_nodes if nid not in fat_assignments]
    if unassigned_fats:
        log.append(f"Unassigned FAT count: {len(unassigned_fats)}")

    log.append(f"Connected Cables total: {len(connected_cable_ids)}")
    log.append(f"Recognized FAT total: {len(fat_assignments)}")
    log.append(f"Assignments: {len(assignments)}")

    if not assignments:
        summary = '\n'.join(log)
        show_log_dialog('Site Co-Design', '未找到需要命名的接入点\n\n' + summary)
        return

    line_write = line_field_idx >= 0
    if not line_write:
        if write_mode == 'addAttr':
            # _ensure_field should have created the field, but if it failed, disable line writes
            log.append(f"Warning: 未能为线图层创建字段 '{target_field_name}'，光缆命名将被跳过")
        else:
            log.append(f"Warning: 线图层字段 '{target_field_name}' 不存在，光缆命名将被跳过")

    try:
        norm_layer.startEditing()
        changes = {fid: {field_idx: name} for fid, name in assignments.items()}
        norm_layer.dataProvider().changeAttributeValues(changes)
        norm_layer.commitChanges()
    except Exception as e:
        try:
            norm_layer.rollBack()
        except Exception:
            pass
        QtWidgets.QMessageBox.critical(None, 'Site Co-Design', f'写入字段失败: {e}')
        return

    if line_write and cable_assignments:
        try:
            if not line_layer.isEditable():
                line_layer.startEditing()
            line_changes = {fid: {line_field_idx: name} for fid, name in cable_assignments.items()}
            line_layer.dataProvider().changeAttributeValues(line_changes)
            line_layer.commitChanges()
        except Exception as e:
            try:
                line_layer.rollBack()
            except Exception:
                pass
            QtWidgets.QMessageBox.critical(None, 'Site Co-Design', f'写入光缆名称失败: {e}')
            return

    summary = '\n'.join(log)
    show_log_dialog('Site Co-Design', f"接入点命名完成，共修改 {len(assignments)} 个要素\n\n" + summary)

# cable_naming_run is kept simple and compatible with previous usage
def cable_naming_run(params: dict, iface=None):
    proj = QgsProject.instance()
    line_layer_name = params.get('line_layer_name')
    conv_layer_name = params.get('conv_point_layer_name')
    norm_layer_name = params.get('norm_point_layer_name')
    if isinstance(norm_layer_name, list):
        norm_layer_names = norm_layer_name
    elif isinstance(norm_layer_name, str):
        norm_layer_names = [norm_layer_name] if norm_layer_name.strip() else []
    else:
        norm_layer_names = []

    if not line_layer_name:
        QtWidgets.QMessageBox.critical(None, 'Site Co-Design', '未指定线图层')
        return

    line_layers = proj.mapLayersByName(line_layer_name)
    if not line_layers:
        QtWidgets.QMessageBox.critical(None, 'Site Co-Design', '未找到指定线图层')
        return
    line_layer = line_layers[0]

    if not conv_layer_name:
        QtWidgets.QMessageBox.critical(None, 'Site Co-Design', '未指定汇聚点图层')
        return
    conv_layers = proj.mapLayersByName(conv_layer_name)
    if not conv_layers:
        QtWidgets.QMessageBox.critical(None, 'Site Co-Design', '未找到指定汇聚点图层')
        return
    conv_layer = conv_layers[0]

    norm_layers = []
    for name in norm_layer_names:
        found = proj.mapLayersByName(name)
        if found:
            norm_layers.append(found[0])
    if not norm_layers:
        QtWidgets.QMessageBox.critical(None, 'Site Co-Design', '未找到指定接入点图层')
        return

    write_mode = params.get('data_write_mode', 'addAttr')
    add_field = params.get('add_attr_name', '')
    mod_field = params.get('modify_attr_name', '')
    target_field_name = mod_field if write_mode == 'modifyAttr' else add_field
    if not target_field_name:
        QtWidgets.QMessageBox.critical(None, 'Site Co-Design', '目标字段未指定')
        return
    if write_mode == 'addAttr':
        _ensure_field(line_layer, target_field_name)

    field_idx = line_layer.fields().indexOf(target_field_name)
    if field_idx < 0:
        QtWidgets.QMessageBox.critical(None, 'Site Co-Design', f'目标字段"{target_field_name}"不存在')
        return

    line_feats = list(line_layer.getFeatures())
    conv_feats = list(conv_layer.getFeatures())
    fat_feats = []
    for norm_layer in norm_layers:
        fat_feats.extend(list(norm_layer.getFeatures()))

    log = []
    log.append(f'Cable count: {len(line_feats)}')
    log.append(f'FDT count: {len(conv_feats)}')
    log.append(f'FAT count: {len(fat_feats)}')
    log.append('==================')

    tc = QgsProject.instance().transformContext()
    try:
        trans_conv_to_line = QgsCoordinateTransform(conv_layer.crs(), line_layer.crs(), tc)
    except Exception:
        trans_conv_to_line = None

    fdt_nodes = _cns_build_nodes(conv_feats, 'FDT', transform=trans_conv_to_line, field_name=params.get('conv_point_layer_field_name'))

    fat_nodes = {}
    for idx, norm_layer in enumerate(norm_layers):
        try:
            trans_norm_to_line = QgsCoordinateTransform(norm_layer.crs(), line_layer.crs(), tc)
        except Exception:
            trans_norm_to_line = None
        prefix = f"norm{idx}"
        fat_nodes.update(_cns_build_nodes(list(norm_layer.getFeatures()), 'FAT', transform=trans_norm_to_line, id_prefix=prefix, log=log))

    nodes = {}
    nodes.update(fdt_nodes)
    nodes.update(fat_nodes)
    point_node_map = {}
    for node_id, node in nodes.items():
        key = _cns_point_key(node['geom'])
        if key is None:
            continue
        point_node_map.setdefault(key, []).append(node_id)

    connection_tol = _cns_connection_tolerance(line_layer.crs())
    cable_assignments = _cns_generate_cable_names(line_feats, nodes, connection_tol, point_node_map, log)
    log.append(f'Generated Cable Assignments: {len(cable_assignments)}')

    if not cable_assignments:
        summary = '\n'.join(log)
        show_log_dialog('Site Co-Design', '未找到需要命名的光缆\n\n' + summary)
        return

    try:
        if not line_layer.isEditable():
            line_layer.startEditing()
        changes = {fid: {field_idx: name} for fid, name in cable_assignments.items()}
        line_layer.dataProvider().changeAttributeValues(changes)
        line_layer.commitChanges()
    except Exception as e:
        try:
            line_layer.rollBack()
        except Exception:
            pass
        QtWidgets.QMessageBox.critical(None, 'Site Co-Design', f'写入字段失败: {e}')
        return

    summary = '\n'.join(log)
    show_log_dialog('Site Co-Design', f'光缆命名完成，共修改 {len(cable_assignments)} 个要素\n\n' + summary)


def cable_split_run(params: dict, iface=None):
    """
    Strict topology cable split integrated into plugin.
    Accepts:
      - line_layer_name: str
      - point_layer_names: list[str] or comma/semicolon separated str
      - output_path: optional path
    """
    proj = QgsProject.instance()
    line_layer_name = params.get('line_layer_name')
    point_layer_names = params.get('point_layer_names')
    output_path = params.get('output_path', '').strip()

    # normalize point layer names
    if isinstance(point_layer_names, list):
        point_names = point_layer_names
    elif isinstance(point_layer_names, str):
        if ',' in point_layer_names:
            point_names = [s.strip() for s in point_layer_names.split(',') if s.strip()]
        elif ';' in point_layer_names:
            point_names = [s.strip() for s in point_layer_names.split(';') if s.strip()]
        elif point_layer_names.strip() == '':
            point_names = []
        else:
            point_names = [point_layer_names.strip()]
    else:
        point_names = []

    if not line_layer_name or not point_names:
        QtWidgets.QMessageBox.critical(None, 'Site Co-Design', '缺少必要图层参数（线图层或点图层）')
        return

    line_layers = proj.mapLayersByName(line_layer_name)
    if not line_layers:
        QtWidgets.QMessageBox.critical(None, 'Site Co-Design', '未找到指定线图层')
        return
    line_layer = line_layers[0]

    # gather point layers
    point_layers = []
    missing = []
    for n in point_names:
        found = proj.mapLayersByName(n)
        if not found:
            missing.append(n)
            continue
        point_layers.append(found[0])
    if not point_layers:
        QtWidgets.QMessageBox.critical(None, 'Site Co-Design', f'未找到任何点图层: {", ".join(missing)}')
        return

    # prepare transform context
    try:
        tc = proj.transformContext()
    except Exception:
        tc = None

    # helper to get a label
    def _point_label(feature):
        try:
            if 'Name' in [f.name() for f in feature.fields()]:
                lab = feature.attribute('Name')
                if lab is not None and str(lab).strip() != '':
                    return str(lab)
            for f in feature.fields():
                if f.typeName() == 'String':
                    lab = feature.attribute(f.name())
                    if lab is not None and str(lab).strip() != '':
                        return str(lab)
        except Exception:
            pass
        return str(feature.id())

    # build spatial index of all points transformed to line CRS
    pt_index = QgsSpatialIndex()
    pt_map = {}
    uid = 1
    for pl in point_layers:
        for pf in pl.getFeatures():
            pg = pf.geometry()
            if pg is None or pg.isEmpty():
                continue
            if pl.crs() != line_layer.crs() and tc is not None:
                try:
                    pg_t = QgsGeometry(pg)
                    pg_t.transform(QgsCoordinateTransform(pl.crs(), line_layer.crs(), tc))
                except Exception:
                    pg_t = QgsGeometry(pg)
            else:
                pg_t = QgsGeometry(pg)
            temp = QgsFeature()
            temp.setGeometry(pg_t)
            try:
                temp.setId(uid)
            except Exception:
                pass
            try:
                pt_index.addFeature(temp)
            except Exception:
                try:
                    pt_index.insertFeature(temp)
                except Exception:
                    pass
            try:
                key = int(temp.id())
            except Exception:
                key = uid
            pt_map[key] = (pf, pg_t, pl.name(), _point_label(pf))
            uid += 1

    # logging header
    log_lines = []
    line_feats = list(line_layer.getFeatures())
    total_points = sum(1 for _ in (f for layer in point_layers for f in layer.getFeatures()))
    log_lines.append('========================================')
    log_lines.append(f'Input Lines : {len(line_feats)}')
    log_lines.append(f'Point Layers : {len(point_layers)}')
    if missing:
        log_lines.append(f'Warning: missing point layers: {", ".join(missing)}')
    log_lines.append('')

    # prepare output layer
    segment_layer = _build_segment_layer(line_layer, layer_name='SEGMENT BREAK')
    output_features = []

    total_output_segments = 0
    total_split_points = 0
    expected_total_segments = 0
    failed_lines = []

    for idx, lf in enumerate(line_feats):
        lg = lf.geometry()
        if lg is None or lg.isEmpty():
            log_lines.append('----------------------------------------')
            log_lines.append(f'Line {idx}')
            log_lines.append('Points Found : 0')
            log_lines.append('Split Points : 0')
            log_lines.append('Expected Segments : 1')
            log_lines.append('Actual Segments : 0')
            log_lines.append('Result : FAIL')
            continue

        # get line endpoints
        line_start_geom = None
        line_end_geom = None
        try:
            geom_const = lg.constGet()
            if hasattr(geom_const, 'startPoint') and hasattr(geom_const, 'endPoint'):
                sp = geom_const.startPoint()
                ep = geom_const.endPoint()
                line_start_geom = QgsGeometry.fromPointXY(QgsPointXY(sp))
                line_end_geom = QgsGeometry.fromPointXY(QgsPointXY(ep))
        except Exception:
            pass
        if line_start_geom is None or line_end_geom is None:
            try:
                if lg.isMultipart():
                    parts = lg.asMultiPolyline()
                    if parts and parts[0] and parts[-1]:
                        line_start_geom = QgsGeometry.fromPointXY(QgsPointXY(parts[0][0]))
                        line_end_geom = QgsGeometry.fromPointXY(QgsPointXY(parts[-1][-1]))
                else:
                    pts = lg.asPolyline()
                    if pts and len(pts) >= 2:
                        line_start_geom = QgsGeometry.fromPointXY(QgsPointXY(pts[0]))
                        line_end_geom = QgsGeometry.fromPointXY(QgsPointXY(pts[-1]))
            except Exception:
                pass

        # candidate points by bbox
        try:
            bbox = lg.boundingBox()
            candidate_ids = pt_index.intersects(bbox)
        except Exception:
            candidate_ids = []

        points_on_line = []
        for cid in candidate_ids:
            entry = pt_map.get(cid)
            if entry is None:
                continue
            pf, pg_t, layer_name, label = entry
            if pg_t is None or pg_t.isEmpty():
                continue
            try:
                if lg.intersects(pg_t):
                    pt_xy = None
                    try:
                        geom_type = pg_t.type()
                        if geom_type == QgsWkbTypes.PointGeometry:
                            if pg_t.isMultipart():
                                mpts = pg_t.asMultiPoint()
                                if mpts:
                                    pt_xy = QgsPointXY(mpts[0])
                            else:
                                p = pg_t.asPoint()
                                pt_xy = QgsPointXY(p)
                        else:
                            cent = pg_t.centroid()
                            if cent is not None and not cent.isEmpty():
                                try:
                                    pt_xy = QgsPointXY(cent.asPoint())
                                except Exception:
                                    pt_xy = None
                    except Exception:
                        pt_xy = None

                    if pt_xy is None:
                        continue
                    try:
                        pos = lg.lineLocatePoint(QgsGeometry.fromPointXY(pt_xy))
                    except Exception:
                        pos = None
                    if pos is None:
                        continue
                    points_on_line.append((pos, label, pf.id(), pt_xy))
            except Exception:
                continue

        points_on_line.sort(key=lambda x: x[0])
        N = len(points_on_line)

        # detect start/end present
        start_exist = False
        end_exist = False
        if N > 0 and line_start_geom is not None and line_end_geom is not None:
            first_pt_geom = QgsGeometry.fromPointXY(points_on_line[0][3])
            last_pt_geom = QgsGeometry.fromPointXY(points_on_line[-1][3])
            try:
                start_exist = line_start_geom.equals(first_pt_geom) or line_start_geom.intersects(first_pt_geom)
            except Exception:
                start_exist = False
            try:
                end_exist = line_end_geom.equals(last_pt_geom) or line_end_geom.intersects(last_pt_geom)
            except Exception:
                end_exist = False

        split_count = N - (1 if start_exist else 0) - (1 if end_exist else 0)
        if split_count < 0:
            split_count = 0
        expected_segments = 1 if N == 0 else (split_count + 1)
        # accumulate expected total segments for final diagnostic
        expected_total_segments += expected_segments

        # build split points to use
        split_pts = []
        split_labels = []
        if N > 0:
            start_index = 1 if start_exist else 0
            end_index = N - 1 if end_exist else N
            for i in range(start_index, end_index):
                split_pts.append(points_on_line[i][3])
                split_labels.append(points_on_line[i][1])
        total_split_points += len(split_pts)

        # perform split
        round_debug = []
        segment_geoms = _split_geometry_iterative(QgsGeometry(lg), split_pts, log=round_debug)
        actual_segments = len(segment_geoms)
        total_output_segments += actual_segments

        # logging per-line summary (always present)
        line_id = lf.id()
        log_lines.append('----------------------------------------')
        log_lines.append(f'Line ID : {line_id}')
        # include feature Name field (prefer 'Name', fallback to 'name' or feature id)
        try:
            feature_name = None
            fields = [f.name() for f in lf.fields()]
            if 'Name' in fields:
                val = lf.attribute('Name')
                if val is not None and str(val).strip() != '':
                    feature_name = str(val)
            elif 'name' in fields:
                val = lf.attribute('name')
                if val is not None and str(val).strip() != '':
                    feature_name = str(val)
            if feature_name is None:
                feature_name = str(line_id)
        except Exception:
            feature_name = str(line_id)
        log_lines.append(f'Name : {feature_name}')
        log_lines.append(f'Points Found : {N}')
        log_lines.append(f'Split Points : {len(split_pts)}')
        log_lines.append(f'StartExist : {start_exist}')
        log_lines.append(f'EndExist : {end_exist}')
        log_lines.append(f'Expected Segments : {expected_segments}')
        log_lines.append(f'Actual Segments : {actual_segments}')

        # collect failed line info
        if actual_segments == expected_segments:
            log_lines.append('Result : PASS')
        else:
            log_lines.append('Result : FAIL')
            # include split point labels for diagnostics
            if split_labels:
                log_lines.append('Split Points：')
                for s in split_labels:
                    log_lines.append(s)
            # include detailed per-split attempts for failed lines
            if round_debug:
                log_lines.append('Split Attempts:')
                for rd in round_debug:
                    log_lines.append(rd)
            # record failed summary for final diagnostics
            try:
                failed_lines.append({'line_id': line_id, 'points_found': N, 'split_points': len(split_pts), 'expected': expected_segments, 'actual': actual_segments, 'split_labels': split_labels})
            except Exception:
                pass

        # add output features
        if segment_geoms:
            for geom in segment_geoms:
                feat = _copy_segment_feature(lf, segment_layer.fields(), geom)
                output_features.append(feat)

    # summary
    log_lines.append('========================================')
    # final diagnostic summary
    log_lines.append('Final Summary:')
    log_lines.append(f'Input Lines : {len(line_feats)}')
    log_lines.append(f'Expected Total Segments : {expected_total_segments}')
    log_lines.append(f'Actual Total Segments : {total_output_segments}')
    diff = expected_total_segments - total_output_segments
    log_lines.append(f'Difference : {diff}')
    if failed_lines:
        log_lines.append('')
        log_lines.append('Failed Lines:')
        for f in failed_lines:
            try:
                log_lines.append(f"Line ID={f.get('line_id')} | Points Found={f.get('points_found')} | Split Points={f.get('split_points')} | Expected={f.get('expected')} | Actual={f.get('actual')}")
                if f.get('split_labels'):
                    log_lines.append('  Split Point Labels:')
                    for s in f.get('split_labels'):
                        log_lines.append(f'    {s}')
            except Exception:
                continue

    # write features
    provider = segment_layer.dataProvider()
    if output_features:
        provider.addFeatures(output_features)

    if output_path:
        output_layer = _save_segment_layer(segment_layer, output_path)
        if output_layer is None:
            QtWidgets.QMessageBox.critical(None, 'Site Co-Design', 'Failed to save output layer')
            show_log_dialog('Site Co-Design', '\n'.join(log_lines))
            return
        QgsProject.instance().addMapLayer(output_layer)
    else:
        QgsProject.instance().addMapLayer(segment_layer)

    show_log_dialog('Site Co-Design', '\n'.join(log_lines))


def validation_point_not_on_line(line_layer: QgsVectorLayer, point_layer: QgsVectorLayer, tolerance: float = None):
    """
    Check each point in point_layer and return list of point features whose
    distance to the nearest line in line_layer is greater than tolerance.

    Returns a list of QgsFeature objects from point_layer.
    Adds detailed debug logging in-memory (returned via validation_run).
    """
    proj = QgsProject.instance()
    tc = None
    try:
        tc = proj.transformContext()
    except Exception:
        tc = None

    debug = []
    debug.append('=========================')
    debug.append('Validation Debug Start')
    debug.append('=========================')

    # Layer info
    try:
        debug.append(f'Line Layer: {line_layer.name()}')
    except Exception:
        debug.append('Line Layer: <unknown>')
    try:
        debug.append(f'Point Layer: {point_layer.name()}')
    except Exception:
        debug.append('Point Layer: <unknown>')
    try:
        debug.append(f'Line Feature Count: {line_layer.featureCount()}')
    except Exception:
        debug.append('Line Feature Count: <error>')
    try:
        debug.append(f'Point Feature Count: {point_layer.featureCount()}')
    except Exception:
        debug.append('Point Feature Count: <error>')
    try:
        debug.append(f'Line CRS: {line_layer.crs().authid()}')
    except Exception:
        debug.append('Line CRS: <unknown>')
    try:
        debug.append(f'Point CRS: {point_layer.crs().authid()}')
    except Exception:
        debug.append('Point CRS: <unknown>')

    if tolerance is None:
        try:
            tolerance = _cns_connection_tolerance(line_layer.crs())
        except Exception:
            tolerance = 0.5
    debug.append(f'Current tolerance = {tolerance}')

    # inspect first 10 points
    debug.append('')
    debug.append('--- Sample of first up to 10 points ---')
    try:
        cnt = 0
        for pf in point_layer.getFeatures():
            if cnt >= 10:
                break
            try:
                pid = pf.id()
            except Exception:
                pid = '<no id>'
            # name extraction
            name = ''
            try:
                fields = [f.name() for f in pf.fields()]
                if 'Name' in fields:
                    val = pf.attribute('Name')
                    if val is not None:
                        name = str(val)
                else:
                    for f in pf.fields():
                        if f.typeName() == 'String':
                            v = pf.attribute(f.name())
                            if v is not None and str(v).strip() != '':
                                name = str(v)
                                break
            except Exception:
                name = ''
            # geometry info
            try:
                geom = pf.geometry()
                geom_type = geom.type() if geom is not None else '<no geom>'
                try:
                    wkt = geom.asWkt()[:200]
                except Exception:
                    wkt = '<wkt failed>'
            except Exception:
                geom_type = '<error>'
                wkt = '<error>'
            debug.append(f'Point ID: {pid}')
            debug.append(f'Name: {name}')
            debug.append(f'Geometry Type: {geom_type}')
            debug.append(f'WKT: {wkt}')
            debug.append('')
            cnt += 1
    except Exception:
        debug.append('Failed to enumerate point features for sample')

    abnormal = []
    # Load line geometries into memory for simple nearest-distance checks
    # Keep (feature id, geometry) pairs so we can log nearest line id
    line_geoms = []
    for lf in line_layer.getFeatures():
        try:
            lg = lf.geometry()
            if lg is None or lg.isEmpty():
                continue
            try:
                lid = lf.id()
            except Exception:
                lid = None
            line_geoms.append((lid, lg))
        except Exception:
            continue

    # Line geometry debug summary
    try:
        debug.append('')
        debug.append('--- Line Geometry Debug ---')
        debug.append(f'Loaded Line Count: {len(line_geoms)}')
        for lid, lg in line_geoms[:5]:
            try:
                debug.append(
                    f'Line ID:{lid}, '
                    f'WKB:{lg.wkbType()}, '
                    f'Length:{lg.length()}, '
                    f'Multipart:{lg.isMultipart()}'
                )
                try:
                    debug.append(f'WKT:{lg.asWkt()[:300]}')
                except Exception:
                    debug.append('WKT: <failed to fetch>')
            except Exception as e:
                debug.append(f'Line Debug Error:{e}')
    except Exception:
        # keep going if debug fails
        pass

    # For each point compute nearest line and distance
    sample_count = 0
    for pf in point_layer.getFeatures():
        pg = pf.geometry()
        if pg is None or pg.isEmpty():
            continue
        # transform point geometry to line CRS if needed
        try:
            if point_layer.crs() != line_layer.crs() and tc is not None:
                pg_t = QgsGeometry(pg)
                try:
                    pg_t.transform(QgsCoordinateTransform(point_layer.crs(), line_layer.crs(), tc))
                except Exception:
                    pg_t = QgsGeometry(pg)
            else:
                pg_t = QgsGeometry(pg)
        except Exception:
            pg_t = QgsGeometry(pg)

        pt_xy = _cns_point_xy(pg_t)
        if pt_xy is None:
            continue
        qg_pt = QgsGeometry.fromPointXY(pt_xy)
        min_dist = float('inf')
        nearest_id = None
        for lid, lg in line_geoms:
            try:
                d = lg.distance(qg_pt)
                if d is None:
                    continue
                if d < min_dist:
                    min_dist = d
                    nearest_id = lid
            except Exception:
                continue

        # point display name
        p_name = ''
        try:
            fields = [f.name() for f in pf.fields()]
            if 'Name' in fields:
                val = pf.attribute('Name')
                if val is not None and str(val).strip() != '':
                    p_name = str(val)
            else:
                for f in pf.fields():
                    if f.typeName() == 'String':
                        v = pf.attribute(f.name())
                        if v is not None and str(v).strip() != '':
                            p_name = str(v)
                            break
        except Exception:
            p_name = ''

        # limited per-point debug for first 10 points
        if sample_count < 10:
            try:
                debug.append('')
                debug.append(f'Point Name: {p_name}')
                debug.append(f'Point ID: {pf.id()}')
                debug.append(f'Nearest Cable ID: {nearest_id}')
                debug.append(f'Distance to Cable: {min_dist}')
                debug.append(f'Current Tolerance: {tolerance}')
            except Exception:
                debug.append('Failed to append per-point debug info')

        is_abnormal = False
        if min_dist is None:
            # treat as skip
            pass
        else:
            if min_dist > tolerance:
                abnormal.append(pf)
                is_abnormal = True

        if sample_count < 10:
            try:
                if is_abnormal:
                    debug.append('ABNORMAL')
                else:
                    debug.append('OK')
            except Exception:
                pass
            sample_count += 1

    # show debug dialog from within this function so logs are visible even if no abnormal found
    try:
        show_log_dialog('Validation Debug - Point not on line', '\n'.join(debug))
    except Exception:
        pass

    return abnormal


def validation_point_not_on_vertex(line_layer: QgsVectorLayer, point_layer: QgsVectorLayer, tolerance: float = None):
    """
    For each point in point_layer, find the nearest line in line_layer and
    check whether the point coincides with any vertex of that nearest line
    (within tolerance). If not, the point is considered abnormal.

    Returns a list of QgsFeature objects from point_layer.
    Adds detailed debug logging in-memory.
    """
    proj = QgsProject.instance()
    tc = None
    try:
        tc = proj.transformContext()
    except Exception:
        tc = None

    debug = []
    debug.append('=========================')
    debug.append('Validation Debug Start')
    debug.append('=========================')

    # Layer info
    try:
        debug.append(f'Line Layer: {line_layer.name()}')
    except Exception:
        debug.append('Line Layer: <unknown>')
    try:
        debug.append(f'Point Layer: {point_layer.name()}')
    except Exception:
        debug.append('Point Layer: <unknown>')
    try:
        debug.append(f'Line Feature Count: {line_layer.featureCount()}')
    except Exception:
        debug.append('Line Feature Count: <error>')
    try:
        debug.append(f'Point Feature Count: {point_layer.featureCount()}')
    except Exception:
        debug.append('Point Feature Count: <error>')
    try:
        debug.append(f'Line CRS: {line_layer.crs().authid()}')
    except Exception:
        debug.append('Line CRS: <unknown>')
    try:
        debug.append(f'Point CRS: {point_layer.crs().authid()}')
    except Exception:
        debug.append('Point CRS: <unknown>')

    if tolerance is None:
        try:
            tolerance = _cns_connection_tolerance(line_layer.crs())
        except Exception:
            tolerance = 0.5
    debug.append(f'Current tolerance = {tolerance}')

    # inspect first 10 points
    debug.append('')
    debug.append('--- Sample of first up to 10 points ---')
    try:
        cnt = 0
        for pf in point_layer.getFeatures():
            if cnt >= 10:
                break
            try:
                pid = pf.id()
            except Exception:
                pid = '<no id>'
            # name extraction
            name = ''
            try:
                fields = [f.name() for f in pf.fields()]
                if 'Name' in fields:
                    val = pf.attribute('Name')
                    if val is not None:
                        name = str(val)
                else:
                    for f in pf.fields():
                        if f.typeName() == 'String':
                            v = pf.attribute(f.name())
                            if v is not None and str(v).strip() != '':
                                name = str(v)
                                break
            except Exception:
                name = ''
            # geometry info
            try:
                geom = pf.geometry()
                geom_type = geom.type() if geom is not None else '<no geom>'
                try:
                    wkt = geom.asWkt()[:200]
                except Exception:
                    wkt = '<wkt failed>'
            except Exception:
                geom_type = '<error>'
                wkt = '<error>'
            debug.append(f'Point ID: {pid}')
            debug.append(f'Name: {name}')
            debug.append(f'Geometry Type: {geom_type}')
            debug.append(f'WKT: {wkt}')
            debug.append('')
            cnt += 1
    except Exception:
        debug.append('Failed to enumerate point features for sample')

    if tolerance is None:
        try:
            tolerance = _cns_connection_tolerance(line_layer.crs())
        except Exception:
            tolerance = 0.5

    # Preload line geometries and their vertex lists
    # store (feature id, geometry, verts)
    lines = []  # list of tuples (line_id, lg_geom, [QgsPointXY,...])
    for lf in line_layer.getFeatures():
        try:
            lg = lf.geometry()
            if lg is None or lg.isEmpty():
                continue
            verts = []
            try:
                if lg.isMultipart():
                    parts = lg.asMultiPolyline()
                    for part in parts:
                        for p in part:
                            verts.append(QgsPointXY(p))
                else:
                    for p in lg.asPolyline():
                        verts.append(QgsPointXY(p))
            except Exception:
                # fallback: attempt to sample vertices by converting to WKT
                try:
                    pts = lg.asPolyline()
                    for p in pts:
                        verts.append(QgsPointXY(p))
                except Exception:
                    pass
            try:
                lid = lf.id()
            except Exception:
                lid = None
            lines.append((lid, lg, verts))
        except Exception:
            continue

    abnormal = []
    sample_count = 0
    for pf in point_layer.getFeatures():
        pg = pf.geometry()
        if pg is None or pg.isEmpty():
            continue
        try:
            if point_layer.crs() != line_layer.crs() and tc is not None:
                pg_t = QgsGeometry(pg)
                try:
                    pg_t.transform(QgsCoordinateTransform(point_layer.crs(), line_layer.crs(), tc))
                except Exception:
                    pg_t = QgsGeometry(pg)
            else:
                pg_t = QgsGeometry(pg)
        except Exception:
            pg_t = QgsGeometry(pg)

        pt_xy = _cns_point_xy(pg_t)
        if pt_xy is None:
            continue

        # find nearest line (by distance to geometry)
        nearest_line_id = None
        nearest_verts = None
        min_dist = float('inf')
        qg_pt = QgsGeometry.fromPointXY(pt_xy)
        for lid, lg, verts in lines:
            try:
                d = lg.distance(qg_pt)
                if d is None:
                    continue
                if d < min_dist:
                    min_dist = d
                    nearest_line_id = lid
                    nearest_verts = verts
            except Exception:
                continue

        # point name
        pname = ''
        try:
            fields = [f.name() for f in pf.fields()]
            if 'Name' in fields:
                val = pf.attribute('Name')
                if val is not None and str(val).strip() != '':
                    pname = str(val)
            else:
                for f in pf.fields():
                    if f.typeName() == 'String':
                        v = pf.attribute(f.name())
                        if v is not None and str(v).strip() != '':
                            pname = str(v)
                            break
        except Exception:
            pname = ''

        # limited per-point debug for first 10 points
        if sample_count < 10:
            try:
                debug.append('')
                debug.append(f'Point Name: {pname}')
                debug.append(f'Point ID: {pf.id()}')
                debug.append(f'Nearest Cable ID: {nearest_line_id}')
                debug.append(f'Distance to Cable: {min_dist}')
                debug.append(f'Current Tolerance: {tolerance}')
                if nearest_verts is not None:
                    debug.append(f'Vertex Count: {len(nearest_verts)}')
                    # compute nearest vertex distance
                    vmin = float('inf')
                    for v in nearest_verts:
                        try:
                            d2 = _cns_distance(v, pt_xy)
                            if d2 is None:
                                continue
                            if d2 < vmin:
                                vmin = d2
                        except Exception:
                            continue
                    if vmin == float('inf'):
                        debug.append('Nearest Vertex Distance: <unknown>')
                    else:
                        debug.append(f'Nearest Vertex Distance: {vmin}')
                else:
                    debug.append('Vertex Count: 0')
            except Exception:
                debug.append('Failed to append per-point debug info')

        is_abnormal = False
        if nearest_verts is None:
            abnormal.append(pf)
            is_abnormal = True
        else:
            # check if any vertex coincides with point within tolerance
            matched = False
            vmin = float('inf')
            for v in nearest_verts:
                try:
                    d_v = _cns_distance(v, pt_xy)
                    if d_v is None:
                        continue
                    if d_v < vmin:
                        vmin = d_v
                    if d_v <= tolerance:
                        matched = True
                        break
                except Exception:
                    continue
            if not matched:
                abnormal.append(pf)
                is_abnormal = True

        if sample_count < 10:
            try:
                if is_abnormal:
                    debug.append('ABNORMAL')
                else:
                    debug.append('OK')
            except Exception:
                pass
            sample_count += 1

    # show debug dialog from within this function so logs are visible even if no abnormal found
    try:
        show_log_dialog('Validation Debug - Point not on cable vertex', '\n'.join(debug))
    except Exception:
        pass

    # attach debug to abnormal features for retrieval
    try:
        for f in abnormal:
            try:
                setattr(f, '_validation_debug', '\n'.join(debug))
            except Exception:
                pass
    except Exception:
        pass

    return abnormal


def validation_run(params: dict, iface=None):
    """
    Unified validation entry point. Reads params, dispatches to specific
    validation_* functions, and handles UI output / selection.

    Expected params keys (from dialog):
      - line_layer_name: str
      - point_layer_name: str
      - validation_item: str (display text of validation)
      - tolerance: optional float
      - list_names: optional bool
      - select_results: optional bool
    """
    proj = QgsProject.instance()
    line_name = params.get('line_layer_name')
    point_name = params.get('point_layer_name')
    item = params.get('validation_item')
    tol = params.get('tolerance')
    try:
        tol = float(tol) if tol is not None and str(tol).strip() != '' else None
    except Exception:
        tol = None

    if not line_name:
        QtWidgets.QMessageBox.critical(None, 'Site Co-Design', '未指定线图层名称')
        return
    if not point_name:
        QtWidgets.QMessageBox.critical(None, 'Site Co-Design', '未指定点图层名称')
        return

    line_layers = proj.mapLayersByName(line_name)
    if not line_layers:
        QtWidgets.QMessageBox.critical(None, 'Site Co-Design', f'未找到线图层: {line_name}')
        return
    point_layers = proj.mapLayersByName(point_name)
    if not point_layers:
        QtWidgets.QMessageBox.critical(None, 'Site Co-Design', f'未找到点图层: {point_name}')
        return

    line_layer = line_layers[0]
    point_layer = point_layers[0]

    # dispatch
    func = None
    if item == 'Point not on line':
        func = validation_point_not_on_line
    elif item == 'Point not on cable vertex':
        func = validation_point_not_on_vertex
    else:
        QtWidgets.QMessageBox.critical(None, 'Site Co-Design', f'未知的校验项: {item}')
        return

    try:
        abnormal_feats = func(line_layer, point_layer, tolerance=tol)
    except Exception as e:
        QtWidgets.QMessageBox.critical(None, 'Site Co-Design', f'Validation 运行出错: {str(e)}')
        return

    # collect debug text if attached to features
    debug_lines = []
    debug_lines.append('=========================')
    debug_lines.append('Validation Debug Start')
    debug_lines.append('=========================')
    try:
        debug_lines.append(f'Line Layer: {line_layer.name()}')
    except Exception:
        debug_lines.append('Line Layer: <unknown>')
    try:
        debug_lines.append(f'Point Layer: {point_layer.name()}')
    except Exception:
        debug_lines.append('Point Layer: <unknown>')
    try:
        debug_lines.append(f'Line Feature Count: {line_layer.featureCount()}')
    except Exception:
        debug_lines.append('Line Feature Count: <error>')
    try:
        debug_lines.append(f'Point Feature Count: {point_layer.featureCount()}')
    except Exception:
        debug_lines.append('Point Feature Count: <error>')
    try:
        debug_lines.append(f'Line CRS: {line_layer.crs().authid()}')
    except Exception:
        debug_lines.append('Line CRS: <unknown>')
    try:
        debug_lines.append(f'Point CRS: {point_layer.crs().authid()}')
    except Exception:
        debug_lines.append('Point CRS: <unknown>')
    debug_lines.append('')

    # append any debug attached to abnormal features
    try:
        if abnormal_feats:
            # get debug from first abnormal (they all carry same debug text)
            f0 = abnormal_feats[0]
            try:
                txt = getattr(f0, '_validation_debug', None)
                if txt:
                    debug_lines.append(txt)
            except Exception:
                pass
    except Exception:
        pass

    # Additionally, run both checks to report counts for both items (debug only)
    try:
        line_abnorm = validation_point_not_on_line(line_layer, point_layer, tolerance=tol)
        vertex_abnorm = validation_point_not_on_vertex(line_layer, point_layer, tolerance=tol)
        debug_lines.append('')
        debug_lines.append(f'Point not on line abnormal count: {len(line_abnorm)}')
        debug_lines.append(f'Point not on vertex abnormal count: {len(vertex_abnorm)}')
    except Exception:
        pass

    # format output
    lines = []
    lines.append('Validation Result')
    lines.append(item)
    lines.append('')
    lines.append(f'Abnormal Count : {len(abnormal_feats)}')
    lines.append('')

    # helper to extract display name
    def _feat_display_name(feat):
        try:
            fields = [f.name() for f in feat.fields()]
            if 'Name' in fields:
                val = feat.attribute('Name')
                if val is not None and str(val).strip() != '':
                    return str(val)
            for f in feat.fields():
                if f.typeName() == 'String':
                    v = feat.attribute(f.name())
                    if v is not None and str(v).strip() != '':
                        return str(v)
        except Exception:
            pass
        return str(feat.id())

    for f in abnormal_feats:
        try:
            lines.append(_feat_display_name(f))
        except Exception:
            try:
                lines.append(str(f.id()))
            except Exception:
                lines.append('<unknown>')

    # show log
    # combine debug_lines + main lines
    try:
        all_text = '\n'.join(debug_lines) + '\n\n' + '\n'.join(lines)
    except Exception:
        all_text = '\n'.join(lines)
    show_log_dialog('Site Co-Design - Validation', all_text)

    # selection if requested
    try:
        if params.get('select_results'):
            # clear existing selection then select abnormal ids
            try:
                point_layer.removeSelection()
            except Exception:
                pass
            ids = [f.id() for f in abnormal_feats]
            try:
                point_layer.selectByIds(ids)
            except Exception:
                try:
                    point_layer.selectByIds(list(map(int, ids)))
                except Exception:
                    pass
    except Exception:
        pass
