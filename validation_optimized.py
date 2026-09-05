# -*- coding: utf-8 -*-
"""Optimized validation routines for ODN Tools Pro.

This module keeps the public validation_run(params, iface=None) signature used by
existing dialogs, but avoids the old implementation's repeated full scans.
It uses a spatial index for nearest-line lookup and only runs the validation
chosen by the user.
"""

from qgis.PyQt import QtWidgets
from qgis.core import (
    QgsProject,
    QgsGeometry,
    QgsPointXY,
    QgsSpatialIndex,
    QgsCoordinateTransform,
    QgsWkbTypes,
)


def _is_geographic(crs):
    try:
        return bool(crs.isGeographic())
    except Exception:
        return False


def _default_tolerance(line_layer):
    return 1e-5 if _is_geographic(line_layer.crs()) else 0.5


def _point_xy(geom):
    if geom is None or geom.isEmpty():
        return None
    try:
        if QgsWkbTypes.geometryType(geom.wkbType()) == QgsWkbTypes.PointGeometry:
            if geom.isMultipart():
                pts = geom.asMultiPoint()
                return QgsPointXY(pts[0]) if pts else None
            return QgsPointXY(geom.asPoint())
        centroid = geom.centroid()
        if centroid is not None and not centroid.isEmpty():
            return QgsPointXY(centroid.asPoint())
    except Exception:
        return None
    return None


def _display_name(feature):
    try:
        field_names = {f.name() for f in feature.fields()}
        if 'Name' in field_names:
            value = feature.attribute('Name')
            if value is not None and str(value).strip():
                return str(value)
        for field in feature.fields():
            if field.typeName() == 'String':
                value = feature.attribute(field.name())
                if value is not None and str(value).strip():
                    return str(value)
    except Exception:
        pass
    try:
        return str(feature.id())
    except Exception:
        return '<unknown>'


def _prepare_lines(line_layer):
    """Build one spatial index and a feature/geometry lookup in line CRS."""
    index = QgsSpatialIndex()
    line_map = {}
    for feature in line_layer.getFeatures():
        geometry = feature.geometry()
        if geometry is None or geometry.isEmpty():
            continue
        try:
            index.addFeature(feature)
        except Exception:
            try:
                index.insertFeature(feature)
            except Exception:
                continue
        line_map[feature.id()] = geometry
    return index, line_map


def _transform_point_geometry(point_layer, line_layer, geometry, transform):
    if geometry is None or geometry.isEmpty():
        return None
    try:
        transformed = QgsGeometry(geometry)
        if point_layer.crs() != line_layer.crs() and transform is not None:
            transformed.transform(transform)
        return transformed
    except Exception:
        return None


def _nearest_line(point_xy, line_index, line_map):
    try:
        candidate_ids = line_index.nearestNeighbor(point_xy, 1)
    except Exception:
        candidate_ids = []
    if not candidate_ids:
        return None, None

    point_geometry = QgsGeometry.fromPointXY(point_xy)
    nearest_id = None
    nearest_geometry = None
    min_distance = float('inf')
    for feature_id in candidate_ids:
        geometry = line_map.get(feature_id)
        if geometry is None:
            continue
        try:
            distance = geometry.distance(point_geometry)
        except Exception:
            continue
        if distance < min_distance:
            min_distance = distance
            nearest_id = feature_id
            nearest_geometry = geometry
    return nearest_id, nearest_geometry


def _line_vertices(geometry):
    vertices = []
    try:
        if geometry.isMultipart():
            for part in geometry.asMultiPolyline():
                vertices.extend(QgsPointXY(point) for point in part)
        else:
            vertices.extend(QgsPointXY(point) for point in geometry.asPolyline())
    except Exception:
        return []
    return vertices


def _debug_header(line_layer, point_layer, tolerance, selected_item):
    return [
        '=========================',
        'Validation Debug Start',
        '=========================',
        f'Validation: {selected_item}',
        f'Line Layer: {line_layer.name()}',
        f'Point Layer: {point_layer.name()}',
        f'Line Feature Count: {line_layer.featureCount()}',
        f'Point Feature Count: {point_layer.featureCount()}',
        f'Line CRS: {line_layer.crs().authid()}',
        f'Point CRS: {point_layer.crs().authid()}',
        f'Tolerance: {tolerance}',
    ]


def validation_point_not_on_line(line_layer, point_layer, tolerance=None):
    tolerance = _default_tolerance(line_layer) if tolerance is None else tolerance
    transform = None
    try:
        transform = QgsCoordinateTransform(
            point_layer.crs(), line_layer.crs(), QgsProject.instance().transformContext()
        )
    except Exception:
        pass

    line_index, line_map = _prepare_lines(line_layer)
    abnormal = []
    debug = _debug_header(line_layer, point_layer, tolerance, 'Point not on line')
    debug.append(f'Indexed valid line geometries: {len(line_map)}')

    for feature in point_layer.getFeatures():
        point_geometry = _transform_point_geometry(point_layer, line_layer, feature.geometry(), transform)
        point_xy = _point_xy(point_geometry)
        if point_xy is None:
            continue
        line_id, line_geometry = _nearest_line(point_xy, line_index, line_map)
        if line_geometry is None:
            abnormal.append(feature)
            continue
        try:
            distance = line_geometry.distance(QgsGeometry.fromPointXY(point_xy))
        except Exception:
            continue
        if distance > tolerance:
            abnormal.append(feature)

    debug.append(f'Abnormal Count: {len(abnormal)}')
    return abnormal, debug


def validation_point_not_on_vertex(line_layer, point_layer, tolerance=None):
    tolerance = _default_tolerance(line_layer) if tolerance is None else tolerance
    transform = None
    try:
        transform = QgsCoordinateTransform(
            point_layer.crs(), line_layer.crs(), QgsProject.instance().transformContext()
        )
    except Exception:
        pass

    line_index, line_map = _prepare_lines(line_layer)
    abnormal = []
    debug = _debug_header(line_layer, point_layer, tolerance, 'Point not on cable vertex')
    debug.append(f'Indexed valid line geometries: {len(line_map)}')

    for feature in point_layer.getFeatures():
        point_geometry = _transform_point_geometry(point_layer, line_layer, feature.geometry(), transform)
        point_xy = _point_xy(point_geometry)
        if point_xy is None:
            continue
        line_id, line_geometry = _nearest_line(point_xy, line_index, line_map)
        if line_geometry is None:
            abnormal.append(feature)
            continue

        vertices = _line_vertices(line_geometry)
        matched = False
        nearest_vertex_distance = float('inf')
        for vertex in vertices:
            try:
                distance = vertex.distance(point_xy)
            except Exception:
                continue
            nearest_vertex_distance = min(nearest_vertex_distance, distance)
            if distance <= tolerance:
                matched = True
                break
        if not matched:
            abnormal.append(feature)

    debug.append(f'Abnormal Count: {len(abnormal)}')
    return abnormal, debug


def _show_result(item, abnormal, debug, list_names, select_results, point_layer):
    lines = list(debug)
    lines.extend(['', 'Validation Result', item, '', f'Abnormal Count : {len(abnormal)}', ''])
    if list_names:
        for feature in abnormal:
            lines.append(_display_name(feature))

    try:
        if select_results:
            point_layer.removeSelection()
            point_layer.selectByIds([feature.id() for feature in abnormal])
    except Exception:
        pass

    try:
        QtWidgets.QMessageBox.information(
            None,
            'ODN Tools Pro - Validation',
            '\n'.join(lines),
        )
    except Exception:
        pass
    return abnormal


def validation_run(params, iface=None):
    """Drop-in replacement for the original validation_run()."""
    project = QgsProject.instance()
    line_name = params.get('line_layer_name')
    point_name = params.get('point_layer_name')
    item = params.get('validation_item')

    if not line_name or not point_name:
        QtWidgets.QMessageBox.critical(None, 'ODN Tools Pro', '请先选择线图层和点图层')
        return

    line_layers = project.mapLayersByName(line_name)
    point_layers = project.mapLayersByName(point_name)
    if not line_layers or not point_layers:
        QtWidgets.QMessageBox.critical(None, 'ODN Tools Pro', '未找到指定图层')
        return

    line_layer = line_layers[0]
    point_layer = point_layers[0]

    try:
        tolerance_value = params.get('tolerance')
        tolerance = float(tolerance_value) if tolerance_value not in (None, '') else None
    except Exception:
        tolerance = None

    if item == 'Point not on line':
        abnormal, debug = validation_point_not_on_line(line_layer, point_layer, tolerance)
    elif item == 'Point not on cable vertex':
        abnormal, debug = validation_point_not_on_vertex(line_layer, point_layer, tolerance)
    else:
        QtWidgets.QMessageBox.critical(None, 'ODN Tools Pro', f'未知的校验项: {item}')
        return

    return _show_result(
        item,
        abnormal,
        debug,
        bool(params.get('list_names')),
        bool(params.get('select_results')),
        point_layer,
    )
