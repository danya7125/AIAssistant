from UM.Application import Application
from PyQt5.QtCore import QThread


class MaterialTools:
    def _context(self):
        app = Application.getInstance()
        if QThread.currentThread() != app.thread():
            raise RuntimeError('Материалы доступны только из главного потока Qt.')
        machine = app.getGlobalContainerStack()
        extruder = app.getExtruderManager().getActiveExtruderStack()
        if machine is None or extruder is None:
            raise RuntimeError('Выберите принтер и активный экструдер.')
        nodes = app.getMaterialManager().getAvailableMaterialsForMachineExtruder(machine, extruder)
        if nodes is None:
            raise RuntimeError('Для выбранного экструдера список материалов недоступен.')
        return app, machine, extruder, nodes

    def _compatibility(self, metadata):
        value = metadata.get('compatible')
        if isinstance(value, bool):
            return value
        if str(value).lower() in ('true', '1'):
            return True
        if str(value).lower() in ('false', '0'):
            return False
        return None

    def _removed(self, metadata):
        return str(metadata.get('removed', False)).lower() in ('true', '1')

    def list_materials(self, query='', offset=0, limit=30):
        if not isinstance(query, str) or type(offset) is not int or offset < 0:
            raise ValueError('query должен быть строкой; offset >= 0.')
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError('limit от 1 до 50.')
        app, machine, extruder, nodes = self._context()
        items = []
        for material_id, node in nodes.items():
            meta = node.getMetadata()
            if self._removed(meta):
                continue
            fields = {key: str(meta.get(key, '') or '')[:2000]
                      for key in ('name', 'brand', 'material', 'color_name', 'description', 'adhesion_info')}
            if query.lower() not in (' '.join(fields.values()) + ' ' + material_id).lower():
                continue
            compatible = self._compatibility(meta)
            items.append(dict(fields, material_id=material_id, compatible=compatible,
                              selectable=compatible is not False))
        items.sort(key=lambda item: (item['brand'], item['name'], item['material_id']))
        current = extruder.material
        return {'printer_id': machine.getId(), 'extruder_id': extruder.getId(),
                'current_material': {'name': current.getName(), 'id': current.getId(),
                                     'root_id': current.getMetaDataEntry('base_file', current.getId())},
                'total': len(items), 'offset': offset, 'materials': items[offset:offset + limit],
                'note': 'Совместимость профиля не подтверждает Tg, УФ-стойкость или прочность детали.'}

    def set_material(self, material_id, extruder_id, printer_id):
        if not all(isinstance(v, str) and v for v in (material_id, extruder_id, printer_id)):
            raise ValueError('Нужны material_id, extruder_id и printer_id из list_materials.')
        app, machine, extruder, nodes = self._context()
        if machine.getId() != printer_id or extruder.getId() != extruder_id:
            raise ValueError('Принтер или активный экструдер изменился. Повторите list_materials.')
        if material_id not in nodes:
            raise ValueError('Материал отсутствует в доступном списке: ' + material_id)
        node = nodes[material_id]
        metadata = node.getMetadata()
        if self._removed(metadata) or self._compatibility(metadata) is False:
            raise ValueError('Профиль материала удалён или несовместим с текущей конфигурацией.')
        target = node.getContainer()
        if target is None:
            raise RuntimeError('Не удалось загрузить профиль материала.')
        before = {'id': extruder.material.getId(), 'name': extruder.material.getName()}
        if before['id'] == target.getId():
            return {'changes': [], 'material': before, 'requires_user_action': False,
                    'note': 'Этот материал уже выбран.'}
        position = next((str(k) for k, value in machine.extruders.items()
                         if value.getId() == extruder_id), None)
        if position is None:
            raise RuntimeError('Не удалось определить позицию экструдера.')
        manager = app.getMachineManager()
        needs_dialog = bool(manager.hasUserSettings) and app.getPreferences().getValue('steslicer/active_mode') == 1
        error = None
        try:
            manager.setMaterial(position, node)
        except Exception as exc:
            error = str(exc)
        actual = machine.extruders[position].material
        if actual.getId() != target.getId():
            raise RuntimeError('Слайсер не подтвердил выбор материала. Текущий: {0}. {1}'.format(actual.getName(), error or ''))
        warnings = []
        if error:
            warnings.append('Материал изменён, но обновление профиля завершилось ошибкой: ' + error)
        if needs_dialog:
            warnings.append('В STE Slicer завершите диалог сохранения/сброса пользовательских настроек, затем продолжите.')
        result = {'changes': [{'key': 'material', 'before': before['name'], 'after': actual.getName()}],
                  'material': {'id': actual.getId(), 'name': actual.getName(), 'root_id': material_id},
                  'requires_user_action': needs_dialog or bool(error), 'warnings': warnings}
        try:
            result['effective_settings'] = {
                key: (extruder if machine.getProperty(key, 'settable_per_extruder') else machine).getProperty(key, 'value')
                for key in ('material_print_temperature', 'material_bed_temperature', 'speed_print', 'layer_height')}
            result['profile'] = extruder.quality.getName()
        except Exception:
            result['warnings'].append('Повторно прочитайте настройки профиля через get_print_settings.')
        return result
