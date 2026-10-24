from UM.Application import Application
from PyQt5.QtCore import QThread


class MaterialTools:
    # Параметры, зависящие от материала, которые нужно сбрасывать из userChanges при смене филамента,
    # чтобы не оставались старые температуры/обдув от предыдущего материала
    MATERIAL_DEPENDENT_KEYS = (
        'material_print_temperature',
        'material_print_temperature_layer_0',
        'material_bed_temperature',
        'material_bed_temperature_layer_0',
        'cool_fan_speed',
        'cool_fan_enabled',
        'retraction_amount',
        'retraction_speed'
    )

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

    def _resolve_material_node(self, material_id, nodes):
        """Находит узел материала по точному ID, ID без учёта регистра или по названию/типу профиля."""
        if not isinstance(material_id, str) or not material_id.strip():
            return None, None
        target_raw = material_id.strip()
        if target_raw in nodes:
            return target_raw, nodes[target_raw]

        target_lower = target_raw.lower()
        # 1. Точное совпадение без учёта регистра по ключу или base_file
        for mid, node in nodes.items():
            meta = node.getMetadata()
            if self._removed(meta):
                continue
            if mid.lower() == target_lower or str(meta.get('base_file', '')).lower() == target_lower:
                return mid, node

        candidates = []
        for mid, node in nodes.items():
            meta = node.getMetadata()
            if self._removed(meta) or self._compatibility(meta) is False:
                continue
            name = str(meta.get('name', '') or '').strip().lower()
            brand = str(meta.get('brand', '') or '').strip().lower()
            if target_lower in (name, (brand + ' ' + name).strip()):
                candidates.append((mid, node))
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            raise ValueError('Несколько профилей с этим именем. Используйте точный material_id из list_materials.')
        return None, None

    def list_materials(self, query='', offset=0, limit=50):
        if not isinstance(query, str) or type(offset) is not int or offset < 0:
            raise ValueError('query должен быть строкой; offset >= 0.')
        if type(limit) is not int or not 1 <= limit <= 150:
            raise ValueError('limit от 1 до 150.')
        app, machine, extruder, nodes = self._context()
        items = []
        for material_id, node in nodes.items():
            meta = node.getMetadata()
            if self._removed(meta):
                continue
            fields = {
                key: str(meta.get(key, '') or '')[:1200]
                for key in ('name', 'brand', 'material', 'color_name', 'description', 'adhesion_info')
            }
            for prop in ('properties_density', 'density', 'properties_diameter'):
                val = meta.get(prop)
                if val:
                    fields[prop] = str(val)

            try:
                container = node.getContainer()
                if container is not None:
                    for param_key in (
                        'default_material_print_temperature',
                        'default_material_bed_temperature',
                        'material_print_temperature',
                        'material_bed_temperature',
                        'cool_fan_speed',
                        'retraction_amount',
                        'retraction_speed',
                        'material_flow'
                    ):
                        val = container.getProperty(param_key, 'value')
                        if val is not None:
                            fields[param_key] = val
            except Exception:
                pass

            if query.lower() not in (' '.join(str(v) for v in fields.values()) + ' ' + material_id).lower():
                continue
            compatible = self._compatibility(meta)
            items.append(dict(
                fields,
                material_id=material_id,
                compatible=compatible,
                selectable=compatible is not False
            ))
        items.sort(key=lambda item: (item['brand'], item['name'], item['material_id']))
        current = extruder.material
        return {
            'printer_id': machine.getId(),
            'extruder_id': extruder.getId(),
            'current_material': {
                'name': current.getName(),
                'id': current.getId(),
                'root_id': current.getMetaDataEntry('base_file', current.getId())
            },
            'total': len(items),
            'offset': offset,
            'materials': items[offset:offset + limit]
        }

    def get_material_details(self, material_id):
        """Возвращает полную карточку материала из Preferences STE Slicer."""
        if not isinstance(material_id, str) or not material_id:
            raise ValueError('material_id должен быть непустой строкой.')
        app, machine, extruder, nodes = self._context()
        resolved_id, node = self._resolve_material_node(material_id, nodes)
        if node is None:
            raise ValueError('Материал не найден: ' + material_id)
        meta = node.getMetadata()
        details = {
            'material_id': resolved_id,
            'name': meta.get('name', ''),
            'brand': meta.get('brand', ''),
            'material_type': meta.get('material', ''),
            'color': meta.get('color_name', ''),
            'description': meta.get('description', ''),
            'adhesion_info': meta.get('adhesion_info', ''),
            'density': meta.get('properties_density', meta.get('density', '')),
            'compatible': self._compatibility(meta)
        }
        try:
            container = node.getContainer()
            if container is not None:
                settings = {}
                for key in (
                    'default_material_print_temperature',
                    'default_material_bed_temperature',
                    'material_print_temperature',
                    'material_bed_temperature',
                    'material_print_temperature_layer_0',
                    'material_bed_temperature_layer_0',
                    'material_flow',
                    'cool_fan_speed',
                    'retraction_amount',
                    'retraction_speed'
                ):
                    val = container.getProperty(key, 'value')
                    if val is not None:
                        settings[key] = val
                details['print_settings'] = settings
        except Exception:
            pass
        return details

    def _clear_stale_material_overrides(self, machine, extruder):
        """Очищает старые переопределения температур/обдува в userChanges при смене материала."""
        for stack in (extruder, machine):
            if stack is None:
                continue
            user_changes = getattr(stack, 'userChanges', None)
            if user_changes is None:
                continue
            try:
                existing = set(user_changes.getAllKeys())
                for key in self.MATERIAL_DEPENDENT_KEYS:
                    if key in existing:
                        user_changes.removeInstance(key)
            except Exception:
                pass

    def set_material(self, material_id, extruder_id=None, printer_id=None):
        if not isinstance(material_id, str) or not material_id.strip():
            raise ValueError('Нужен непустой material_id.')
        app, machine, extruder, nodes = self._context()
        if printer_id is not None and machine.getId() != printer_id:
            raise ValueError('Принтер изменился; перечитайте list_materials.')
        if extruder_id is not None and extruder.getId() != extruder_id:
            raise ValueError('Активный экструдер изменился; перечитайте list_materials.')
        resolved_id, node = self._resolve_material_node(material_id, nodes)
        if node is None:
            raise ValueError('Материал отсутствует в доступном списке: ' + material_id)
        metadata = node.getMetadata()
        if self._removed(metadata) or self._compatibility(metadata) is False:
            raise ValueError('Профиль материала удалён или несовместим с текущей конфигурацией.')
        target = node.getContainer()
        if target is None:
            raise RuntimeError('Не удалось загрузить профиль материала.')
        before = {'id': extruder.material.getId(), 'name': extruder.material.getName()}
        if before['id'] == target.getId():
            return {
                'changes': [],
                'material': {'id': before['id'], 'name': before['name'], 'root_id': resolved_id},
                'requires_user_action': False,
                'note': 'Этот материал уже выбран. Переходите к set_print_settings и start_slicing.'
            }
        actual_extruder_id = extruder.getId()
        position = next(
            (str(k) for k, value in machine.extruders.items() if value.getId() == actual_extruder_id),
            None
        )
        if position is None:
            raise ValueError('Позиция экструдера не определена.')
        manager = app.getMachineManager()
        needs_dialog = bool(manager.hasUserSettings) and app.getPreferences().getValue('steslicer/active_mode') == 1
        error = None
        try:
            manager.setMaterial(position, node)
        except Exception as exc:
            error = str(exc)
        actual = machine.extruders[position].material
        if actual.getId() != target.getId():
            raise RuntimeError(
                'Слайсер не подтвердил выбор материала. Текущий: {0}. {1}'.format(actual.getName(), error or '')
            )
        warnings = []
        if needs_dialog:
            warnings.append('Завершите штатный диалог сохранения/сброса настроек в STE Slicer, затем напишите продолжить.')
        elif not error:
            self._clear_stale_material_overrides(machine, extruder)
        if error:
            warnings.append('Материал изменён, но обновление профиля завершилось ошибкой: ' + error)
        result = {
            'changes': [{'key': 'material', 'before': before['name'], 'after': actual.getName()}],
            'material': {'id': actual.getId(), 'name': actual.getName(), 'root_id': resolved_id},
            'requires_user_action': needs_dialog or bool(error),
            'warnings': warnings
        }
        try:
            result['effective_settings'] = {
                key: (extruder if machine.getProperty(key, 'settable_per_extruder') else machine).getProperty(key, 'value')
                for key in (
                    'material_print_temperature',
                    'material_bed_temperature',
                    'cool_fan_speed',
                    'retraction_amount',
                    'retraction_speed',
                    'speed_print',
                    'layer_height'
                )
            }
            result['profile'] = extruder.quality.getName()
        except Exception:
            result['warnings'].append('Повторно прочитайте настройки профиля через get_print_settings.')
        return result
