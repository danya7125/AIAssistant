"""Read-only checks. Snapshot Qt state on the UI thread; analyse in worker."""
import math
import re
import time
import numpy as np
from UM.Application import Application


def snapshot_geometry():
    app = Application.getInstance()
    stack = app.getGlobalContainerStack()
    if stack is None:
        raise ValueError('Выберите принтер.')
    plate = app.getMultiBuildPlateModel().activeBuildPlate
    keys = ('printing_mode', 'machine_width', 'machine_depth', 'machine_height',
            'cylindrical_mode_base_diameter', 'non_printing_base_diameter')
    settings = {k: stack.getProperty(k, 'value') for k in keys}
    models = []
    for node in app.getController().getScene().getRoot().getAllChildren():
        mesh = node.getMeshData()
        if mesh is None or not node.callDecoration('isSliceable') or node.callDecoration('getBuildPlateNumber') != plate:
            continue
        if mesh.getVertexCount() > 1500000:
            models.append({'name': node.getName(), 'skipped': 'Слишком большая сетка (более 1,5 млн вершин).'})
            continue
        vertices = mesh.getVertices().copy()
        matrix = node.getWorldTransformation().getData().copy()
        indices = mesh.getIndices()
        models.append({'name': node.getName(), 'vertices': vertices,
                       'matrix': matrix, 'indices': None if indices is None else indices.copy(),
                       'outside': getattr(node, '_outside_buildarea', None)})
    return {'settings': settings, 'models': models}


def guard(cancel, deadline):
    if cancel is not None and cancel.is_set():
        raise RuntimeError('Проверка отменена.')
    if deadline is not None and time.monotonic() >= deadline:
        raise RuntimeError('Истёк лимит проверки.')


def analyse_geometry(data, cancel=None, deadline=None):
    report = {'status': 'partial', 'models': [], 'settings': data['settings'],
              'not_checked': ['Самопересечения', 'Манifoldность вершин', 'Минимальная толщина',
                              'Контакт с основанием', 'Столкновения при вращении',
                              'Физическая калибровка', 'Прочность']}
    for model in data['models']:
        guard(cancel, deadline)
        result = {'name': model['name']}
        report['models'].append(result)
        if 'skipped' in model:
            result['not_checked'] = model['skipped']
            continue
        v = model['vertices']
        matrix = model['matrix']
        if len(v) == 0 or not np.isfinite(v).all() or not np.isfinite(matrix).all():
            result['error'] = 'Пустая сетка или нечисловые координаты.'
            continue
        v = v.dot(matrix[:3, :3].T) + matrix[:3, 3]
        result['world_size_mm_y_up'] = (v.max(axis=0) - v.min(axis=0)).tolist()
        result['slicer_outside_buildarea'] = None if model['outside'] is None else bool(model['outside'])
        # Quantisation is approximate; publish tolerance rather than promise exact topology.
        tolerance = 0.00001
        _, inverse = np.unique(np.round(v / tolerance).astype(np.int64), axis=0, return_inverse=True)
        faces = model['indices']
        if faces is None:
            if len(v) % 3:
                result['error'] = 'Число вершин не кратно трём.'
                continue
            faces = np.arange(len(v)).reshape(-1, 3)
        faces = np.asarray(faces)
        if faces.size == 0 or faces.ndim != 2 or faces.shape[1] != 3 or not np.issubdtype(faces.dtype, np.integer) or faces.min() < 0 or faces.max() >= len(v):
            result['error'] = 'Некорректные индексы треугольников.'
            continue
        triangle = v[faces]
        area2 = np.linalg.norm(np.cross(triangle[:,1]-triangle[:,0], triangle[:,2]-triangle[:,0]), axis=1)
        degenerate = area2 <= tolerance * tolerance
        f = inverse[faces[~degenerate]]
        edges = np.sort(np.concatenate((f[:,[0,1]], f[:,[1,2]], f[:,[2,0]])), axis=1)
        _, counts = np.unique(edges, axis=0, return_counts=True)
        result.update(triangles=int(len(faces)), degenerate_triangles=int(degenerate.sum()),
                      open_edges=int((counts == 1).sum()), non_manifold_edges=int((counts > 2).sum()),
                      vertex_quantisation_mm=tolerance)
        guard(cancel, deadline)
    settings = data['settings']
    report['parameter_errors'] = []
    if settings.get('printing_mode') in ('cylindrical', 'cylindrical_full'):
        for key in ('cylindrical_mode_base_diameter', 'non_printing_base_diameter'):
            value = settings.get(key)
            if value is not None and (not isinstance(value, (float,int)) or not math.isfinite(value) or value < 0 or (key == 'cylindrical_mode_base_diameter' and value == 0)):
                report['parameter_errors'].append('Недопустимое значение ' + key)
        report['not_checked'].append('Габарит вращения: требуется подтверждённое преобразование осей конкретного движка и модель креплений')
    if not report['models']:
        report['error'] = 'На активном столе нет моделей.'
    return report


def snapshot_gcode():
    app = Application.getInstance()
    scene = app.getController().getScene()
    plate = app.getMultiBuildPlateModel().activeBuildPlate
    chunks = getattr(scene, 'gcode_dict', {}).get(plate, [])
    return list(chunks) if not isinstance(chunks, str) else [chunks]


def analyse_gcode(chunks, cancel=None, deadline=None):
    report = {'status': 'partial', 'motion_commands': 0, 'coordinate_systems': [],
              'axis_words_range': {}, 'unsupported_commands': [],
              'not_checked': ['Столкновения головки, детали и креплений', 'Пределы физических осей',
                              'Опора новых слоёв', 'Согласование сердечника и оболочки',
                              'Физическая калибровка', 'Актуальность G-code относительно текущей сцены'],
              'note': 'Диапазоны слов G-code не являются физическими координатами: учитывайте G54–G59, G92 и G90/G91.'}
    if not chunks:
        report['error'] = 'G-code отсутствует. Сначала выполните нарезку.'
        return report
    if sum(len(c) for c in chunks) > 50000000:
        report['error'] = 'G-code больше 50 МБ: анализ пропущен.'
        return report
    systems, unsupported = set(), set()
    count = 0
    for chunk in chunks:
        for line in chunk.splitlines():
            count += 1
            if count % 1000 == 0:
                guard(cancel, deadline)
            code = re.sub(r'\([^)]*\)', '', line.split(';',1)[0]).upper()
            commands = re.findall(r'[GM]\s*\d+(?:\.\d+)?', code)
            for command in commands:
                command = command.replace(' ', '')
                number = float(command[1:])
                if command[0] == 'G' and 54 <= number <= 59:
                    systems.add(command)
                if command[0] == 'G' and number in (0,1,2,3):
                    report['motion_commands'] += 1
                if command[0] == 'G' and number in (2,3,28,29,53,92):
                    unsupported.add(command)
            for axis, raw in re.findall(r'([XYZABC])\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))', code):
                value = float(raw)
                bounds = report['axis_words_range'].setdefault(axis, [value,value])
                bounds[0], bounds[1] = min(bounds[0],value), max(bounds[1],value)
    guard(cancel, deadline)
    report['coordinate_systems'] = sorted(systems)
    report['unsupported_commands'] = sorted(unsupported)
    report['lines'] = count
    return report
