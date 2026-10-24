"""Profile-card transport. No Qt imports; scene_stl must run on the Qt thread."""
import json
import struct
import math
import re


def parse_card(value):
    if isinstance(value, str):
        value = value.strip()
        fenced = re.match(r'^```(?:json)?\s*([\s\S]*?)\s*```$', value, re.I)
        if fenced:
            value = fenced.group(1)
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if not isinstance(value, dict) or value.get('action') != 'show_profile_card':
        return None
    if not isinstance(value.get('text'), str) or not isinstance(value.get('model_path'), str):
        return None
    params = value.get('params')
    if not isinstance(params, dict) or not params or len(params) > 60:
        return None
    if any(not isinstance(k, str) or (v is not None and not isinstance(v, (str, int, float, bool)))
           for k, v in params.items()):
        return None
    if any(isinstance(v, float) and not math.isfinite(v) for v in params.values()):
        return None
    return value


def card_from_snapshot(snapshot, text):
    settings = snapshot.get('settings', {})
    context = snapshot.get('context', {})
    mapping = {
        'nozzle': 'machine_nozzle_size',
        'temp_nozzle': 'material_print_temperature',
        'temp_bed': 'material_bed_temperature',
        'layer_height': 'layer_height',
        'speed': 'speed_print',
        'infill': 'infill_sparse_density'
    }
    params = {'material': context.get('material')}
    params.update({name: settings.get(key) for name, key in mapping.items()})
    if not settings:
        raise ValueError('Нет доступного снимка параметров.')
    return {'action': 'show_profile_card', 'text': text, 'model_path': 'FROM_SCENE',
            'params': params, 'applied': True, 'title': context.get('profile') or 'Профиль печати'}


def scene_stl(scene):
    import numpy as np
    from UM.Scene.Selection import Selection
    nodes = scene.nodes()
    selected = Selection.getAllSelectedObjects()
    selected_nodes = [n for n in nodes if n in selected]
    if len(selected_nodes) == 1:
        nodes = selected_nodes
    if not nodes:
        raise ValueError('На активном столе нет модели.')
    if len(nodes) != 1:
        raise ValueError('На столе несколько моделей. Выделите одну для превью.')
    node = nodes[0]
    vertices = np.asarray(scene.points(node), dtype=np.float64)
    indices = node.getMeshData().getIndices()
    if indices is None:
        if len(vertices) % 3:
            raise ValueError('Некорректная треугольная сетка.')
        triangles = vertices.reshape((-1, 3, 3))
    else:
        indices = np.asarray(indices)
        if indices.size % 3 or not np.issubdtype(indices.dtype, np.integer):
            raise ValueError('Некорректные индексы треугольников.')
        if indices.size and (indices.min() < 0 or indices.max() >= len(vertices)):
            raise ValueError('Индекс вершины вне сетки.')
        triangles = vertices[indices.reshape((-1, 3))]
    if not 0 < len(triangles) <= 500000 or not np.isfinite(triangles).all():
        raise ValueError('Пустая, повреждённая или слишком большая сетка (лимит 500000 граней).')
    # Reflection changes winding. Preserve outward normals for mirrored models.
    if np.linalg.det(node.getWorldTransformation().getData()[:3, :3]) < 0:
        triangles = triangles[:, [0, 2, 1], :]
    normals = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
    length = np.linalg.norm(normals, axis=1)
    good = length > 0
    normals[good] /= length[good, None]
    records = np.zeros(len(triangles), dtype=[('normal', '<f4', (3,)),
                       ('vertices', '<f4', (3, 3)), ('attribute', '<u2')])
    records['normal'] = normals
    records['vertices'] = triangles
    if not np.isfinite(records['vertices']).all():
        raise ValueError('Координаты не помещаются в STL.')
    return b'STE Slicer scene; Y up; millimeters'.ljust(80, b'\0') + struct.pack('<I', len(records)) + records.tobytes()
