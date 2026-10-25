from .OrientationAnalysis import analyse as analyse_orientation_mesh
from .SceneTools import SceneTools
from .GeometryChecks import snapshot_geometry, analyse_geometry, snapshot_gcode, analyse_gcode

import math
import threading
import time
import numpy as np
from .SliceMonitor import SliceMonitor
from .MaterialTools import MaterialTools

from PyQt5.QtCore import QThread
from UM.Application import Application


class SettingsTools:
    """Read and modify live STE Slicer settings."""

    MAIN_SETTINGS = [
        "printing_mode",
        "layer_height",
        "layer_height_0",
        "infill_sparse_density",
        "infill_pattern",
        "infill_line_distance",
        "wall_thickness",
        "wall_line_count",
        "outer_inset_first",
        "top_layers",
        "bottom_layers",
        "top_bottom_thickness",
        "skin_outline_count",
        "material_print_temperature",
        "material_print_temperature_layer_0",
        "material_bed_temperature",
        "material_bed_temperature_layer_0",
        "speed_print",
        "speed_wall",
        "speed_infill",
        "speed_topbottom",
        "speed_travel",
        "speed_support",
        "support_enable",
        "support_type",
        "support_tree_enable",
        "support_pattern",
        "support_roof_enable",
        "support_roof_density",
        "support_roof_line_distance",
        "support_top_distance",
        "support_bottom_distance",
        "support_z_distance",
        "support_xy_distance",
        "support_interface_enable",
        "support_interface_density",
        "adhesion_type",
        "brim_width",
        "skirt_brim_minimal_length",
        "draft_shield_enabled",
        "cool_fan_enabled",
        "cool_fan_speed",
        "retraction_enable",
        "retraction_amount",
        "retraction_speed",
        "cylindrical_mode_base_diameter",
        "non_printing_base_diameter",
        "cylindrical_round_segments"
    ]

    # Порядок применения родительских переключателей перед дочерними параметрами
    PARENT_KEYS_PRIORITY = {
        "printing_mode": 0,
        "support_enable": 1,
        "support_tree_enable": 2,
        "support_interface_enable": 3,
        "support_roof_enable": 4,
        "adhesion_type": 5,
        "cool_fan_enabled": 6,
        "retraction_enable": 7
    }

    KEY_ALIASES = {
        "infill_density": "infill_sparse_density",
        "infill_percent": "infill_sparse_density",
        "print_temperature": "material_print_temperature",
        "nozzle_temperature": "material_print_temperature",
        "extruder_temperature": "material_print_temperature",
        "temperature_nozzle": "material_print_temperature",
        "initial_layer_temperature": "material_print_temperature_layer_0",
        "first_layer_temperature": "material_print_temperature_layer_0",
        "bed_temperature": "material_bed_temperature",
        "build_plate_temperature": "material_bed_temperature",
        "first_layer_bed_temperature": "material_bed_temperature_layer_0",
        "initial_layer_bed_temperature": "material_bed_temperature_layer_0",
        "initial_layer_height": "layer_height_0",
        "first_layer_height": "layer_height_0",
        "print_speed": "speed_print",
        "wall_speed": "speed_wall",
        "infill_speed": "speed_infill",
        "travel_speed": "speed_travel",
        "support_speed": "speed_support",
        "top_bottom_speed": "speed_topbottom",
        "fan_speed": "cool_fan_speed",
        "fan_enabled": "cool_fan_enabled",
        "cooling_fan_enabled": "cool_fan_enabled",
        "enable_support": "support_enable",
        "supports_enabled": "support_enable",
        "tree_support": "support_tree_enable",
        "tree_support_enable": "support_tree_enable",
        "platform_adhesion": "adhesion_type",
        "build_plate_adhesion": "adhesion_type",
        "wall_count": "wall_line_count",
        "perimeters": "wall_line_count"
    }

    EDITABLE_TYPES = ("bool", "int", "float", "enum")
    PRINTING_MODES = ("classic", "cylindrical", "cylindrical_full")

    def __init__(self):
        self._scene_tools = SceneTools()
        self._slice_monitor = SliceMonitor()
        self._materials = MaterialTools()
        self._search_cache = {}

    def on_main_thread(self, function, cancel_event=None, deadline=None):
        app = Application.getInstance()
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("Запрос отменён.")
        if deadline is not None and time.monotonic() >= deadline:
            raise RuntimeError("Истёк общий срок задачи.")

        if QThread.currentThread() == app.thread():
            return function()

        done = threading.Event()
        guard = threading.Lock()
        state = {"cancelled": False}

        def execute():
            with guard:
                if (not state["cancelled"]
                        and not (cancel_event is not None and cancel_event.is_set())
                        and not (deadline is not None and time.monotonic() >= deadline)):
                    try:
                        state["result"] = function()
                    except Exception as error:
                        state["error"] = error
                else:
                    state["error"] = RuntimeError("Запрос отменён или срок задачи истёк.")
                done.set()

        app.callLater(execute)

        wait_seconds = min(30, max(0, deadline - time.monotonic())) if deadline else 30
        if not done.wait(wait_seconds):
            with guard:
                if not done.is_set():
                    state["cancelled"] = True
                    raise RuntimeError(
                        "Главный поток слайсера не ответил за 30 секунд."
                    )

        if "error" in state:
            raise state["error"]

        return state.get("result")

    def _stacks(self):
        app = Application.getInstance()

        if QThread.currentThread() != app.thread():
            raise RuntimeError("Требуется главный поток Qt.")

        global_stack = app.getGlobalContainerStack()

        if global_stack is None:
            raise RuntimeError("Сначала выберите принтер.")

        extruder_stack = (
            app.getExtruderManager().getActiveExtruderStack()
        )

        return global_stack, extruder_stack

    def _resolve_key(self, key, available_keys):
        if not isinstance(key, str):
            return None
        clean = key.strip()
        if clean in available_keys:
            return clean
        alias = self.KEY_ALIASES.get(clean.lower())
        if alias and alias in available_keys:
            return alias
        return None

    def _stack(self, key, stacks):
        global_stack, extruder_stack = stacks

        if not isinstance(key, str):
            raise ValueError("Название параметра должно быть строкой.")

        if key not in global_stack.getAllKeys():
            raise ValueError("Параметр недоступен: " + key)

        if (
            extruder_stack is not None
            and global_stack.getProperty(
                key, "settable_per_extruder"
            )
        ):
            return extruder_stack

        return global_stack

    def _metadata(self, key, stack):
        kind = stack.getProperty(key, "type")
        label = stack.getProperty(key, "label")
        unit = stack.getProperty(key, "unit")
        description = stack.getProperty(key, "description")
        options = stack.getProperty(key, "options")

        if kind is None:
            kind = "unknown"

        if not isinstance(kind, str):
            kind = str(kind)

        result = {
            "key": key,
            "label": label if isinstance(label, str) else key,
            "type": kind,
            "unit": unit if isinstance(unit, str) else "",
            "description": (
                description if isinstance(description, str) else ""
            )
        }

        if isinstance(options, dict):
            if key == "printing_mode":
                options = {
                    k: v for k, v in options.items()
                    if k in self.PRINTING_MODES
                }
            result["options"] = options

        for prop in ("minimum_value", "maximum_value"):
            bound = stack.getProperty(key, prop)

            if (
                type(bound) in (int, float)
                and math.isfinite(bound)
            ):
                result[prop] = bound

        return result

    def _editable(self, key, meta):
        if meta["type"] not in self.EDITABLE_TYPES:
            return False

        if key.startswith("machine_"):
            return False

        if "gcode" in key.lower():
            return False

        if key.startswith("mesh_"):
            return False

        if meta["type"] == "enum":
            options = meta.get("options", {})
            if not options:
                return False

        return True

    def read(self, keys=None):
        stacks = self._stacks()
        available = stacks[0].getAllKeys()

        if keys is None:
            keys = [k for k in self.MAIN_SETTINGS if k in available]

        if (
            not isinstance(keys, (list, tuple))
            or any(not isinstance(k, str) for k in keys)
        ):
            raise ValueError(
                "keys должен быть списком названий параметров."
            )

        result = {}

        for raw_key in keys:
            # Строгая проверка: синонимы и выдуманные ключи запрещены
            if raw_key not in available:
                raise ValueError(
                    "Недопустимый параметр '{0}'. Синонимы запрещены. Используйте только настоящие ключи из list_print_settings.".format(raw_key)
                )
            
            # Читаем значение параметра из стека
            stack = self._stack(raw_key, stacks)
            result[raw_key] = stack.getProperty(raw_key, "value")

        return result

    def profile_snapshot(self):
        global_stack, _ = self._stacks()
        profile_keys = (
            "machine_nozzle_size",
            "material_print_temperature",
            "material_bed_temperature",
            "layer_height",
            "speed_print",
            "speed_wall",
            "speed_layer_0",
            "infill_sparse_density",
            "wall_line_count",
            "printing_mode"
        )
        available = global_stack.getAllKeys()
        settings = self.read([
            key for key in profile_keys
            if key in available
        ])
        return {
            "context": self.get_print_context(),
            "settings": settings
        }

    def describe(self, query="", offset=0, limit=30):
        if not isinstance(query, str):
            raise ValueError("query должен быть строкой.")

        if (
            type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= 50
        ):
            raise ValueError(
                "offset >= 0; limit от 1 до 50."
            )

        stacks = self._stacks()
        global_stack = stacks[0]
        search = query.lower()

        found = []

        for key in sorted(global_stack.getAllKeys()):
            cache_key = (global_stack.getId(), key)
            searchable = self._search_cache.get(cache_key)
            if searchable is None:
                stack = self._stack(key, stacks)
                searchable = " ".join([
                    key, str(stack.getProperty(key, "label") or ""),
                    str(stack.getProperty(key, "description") or "")
                ]).lower()
                self._search_cache[cache_key] = searchable

            if search in searchable:
                found.append(key)

        result = []

        for key in found[offset:offset + limit]:
            stack = self._stack(key, stacks)
            meta = self._metadata(key, stack)

            meta["editable"] = self._editable(key, meta)
            meta["value"] = self.read([key])[key]

            enabled = stack.getProperty(key, "enabled")
            meta["enabled"] = bool(enabled)

            result.append(meta)

        return {
            "total": len(found),
            "offset": offset,
            "settings": result
        }

    def _validate(self, key, value, stack):
        """Приводит значение к типу параметра слайсера и проверяет допустимые границы."""
        meta = self._metadata(key, stack)

        if not self._editable(key, meta):
            raise ValueError(
                "Параметр доступен только для чтения: " + key
            )

        kind = meta["type"]

        if kind == "bool":
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                low = value.strip().lower()
                if low in ("true", "1", "yes", "on", "да"):
                    return True
                if low in ("false", "0", "no", "off", "нет"):
                    return False
            if isinstance(value, (int, float)) and value in (0, 1):
                return bool(value)
            raise ValueError(key + ": требуется true или false.")

        elif kind in ("int", "float"):
            if isinstance(value, bool):
                raise ValueError(key + ": требуется число.")
            if isinstance(value, str):
                try:
                    value = float(value.strip().replace(",", "."))
                except Exception:
                    raise ValueError(key + ": требуется число.")
            if not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(key + ": число должно быть конечным.")

            minimum = meta.get("minimum_value")
            maximum = meta.get("maximum_value")

            if minimum is not None and value < minimum:
                raise ValueError(key + ": значение ниже минимума " + str(minimum))
            if maximum is not None and value > maximum:
                raise ValueError(key + ": значение выше максимума " + str(maximum))

            if kind == "int":
                value = int(round(float(value)))
            else:
                value = float(value)

        elif kind == "enum":
            options = meta.get("options", {})
            val_str = str(value).strip() if value is not None else ""
            if val_str not in options:
                matched = None
                for opt_key, opt_label in options.items():
                    if val_str.lower() in (str(opt_key).lower(), str(opt_label).lower()):
                        matched = opt_key
                        break
                if matched is None:
                    raise ValueError(
                        key + ": доступные варианты: " + ", ".join(options)
                    )
                val_str = matched

            if key == "printing_mode" and val_str not in self.PRINTING_MODES:
                raise ValueError("Разрешены только 3D, 5D Spiral и 5D.")
            value = val_str

        return value

    def write(self, values):
        if (
            not isinstance(values, dict)
            or not values
            or len(values) > 60
        ):
            raise ValueError(
                "values должен содержать от 1 до 60 параметров."
            )

        if any(not isinstance(k, str) for k in values):
            raise ValueError(
                "Названия параметров должны быть строками."
            )

        stacks = self._stacks()
        available = stacks[0].getAllKeys()
        resolved_map = {}
        skipped = []

        for raw_key, raw_value in values.items():
            # Строгая проверка: синонимы и выдуманные ключи запрещены
            if raw_key not in available:
                raise ValueError(
                    "Недопустимый параметр '{0}'. Синонимы запрещены. Используйте только настоящие ключи из list_print_settings.".format(raw_key)
                )
            resolved_map[raw_key] = raw_value

        # Если переданы дочерние настройки поддержек (support_tree_enable=True, support_roof_enable=True),
        # автоматически гарантируем, что support_enable тоже включён
        if any(
            resolved_map.get(k) in (True, "true", "True", 1)
            for k in ("support_tree_enable", "support_roof_enable", "support_interface_enable")
        ):
            if "support_enable" in available:
                resolved_map["support_enable"] = True

        ordered_items = sorted(
            resolved_map.items(),
            key=lambda kv: self.PARENT_KEYS_PRIORITY.get(kv[0], 100)
        )

        prepared = []
        normalized_keys = []

        for key, raw_value in ordered_items:
            try:
                stack = self._stack(key, stacks)
                value = self._validate(key, raw_value, stack)
            except Exception as err:
                skipped.append({
                    "key": key,
                    "reason": str(err)
                })
                continue

            container = stack.userChanges
            existed = key in container.getAllKeys()

            old_override = (
                container.getProperty(key, "value")
                if existed else None
            )

            prepared.append((
                key,
                value,
                stack,
                container,
                existed,
                old_override
            ))
            normalized_keys.append(key)

        if not prepared:
            raise ValueError(
                "Ни один из переданных параметров не удалось применить: "
                + "; ".join("{0} ({1})".format(s["key"], s["reason"]) for s in skipped)
            )

        if skipped:
            raise ValueError("Пакет не применён: " + "; ".join(x['key'] + ': ' + x['reason'] for x in skipped))
        before = self.read(normalized_keys)
        applied = []

        try:
            for item in prepared:
                key, value, stack, container, existed, old = item

                applied.append((
                    key, container, existed, old
                ))

                container.setProperty(key, "value", value)

            active_keys = []
            for item in prepared:
                key, value, stack, container, existed, old = item

                state = str(
                    stack.getProperty(key, "validationState")
                )

                if stack.getProperty(key, "enabled") is False:
                    # Если родительский переключатель выключен (например, support_enable=False),
                    # не сбрасываем весь пакет из-за отключенных дочерних опций
                    if key.startswith("support_") and not stack.getProperty("support_enable", "value"):
                        continue
                    if key.startswith("cool_fan_") and not stack.getProperty("cool_fan_enabled", "value"):
                        continue
                    if key.startswith("retraction_") and not stack.getProperty("retraction_enable", "value"):
                        continue
                    if key.startswith("material_bed_temperature"):
                        continue
                    raise ValueError(key + ": неактивен или отклонён; весь пакет откатан.")
                if "Error" in state or "Exception" in state:
                    raise ValueError(key + ": ошибка валидации слайсера; весь пакет откатан.")
                actual = stack.getProperty(key, "value")
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    if not isinstance(actual, (int, float)) or not math.isclose(float(actual), float(value), rel_tol=1e-6, abs_tol=1e-6):
                        raise ValueError(key + ": значение после записи отличается от запрошенного.")
                elif actual != value:
                    raise ValueError(key + ": значение после записи отличается от запрошенного.")

                active_keys.append((key, value))

            after = self.read(normalized_keys)

        except Exception as error:
            failures = []

            for key, container, existed, old in reversed(applied):
                try:
                    if existed:
                        container.setProperty(
                            key, "value", old
                        )
                    else:
                        container.removeInstance(key)
                except Exception:
                    failures.append(key)

            if failures:
                raise RuntimeError(
                    "Ошибка: {}. Не удалось откатить: {}".format(
                        error, ", ".join(failures)
                    )
                )

            raise

        applied_key_set = {k for k, _ in active_keys}
        response = {
            "changes": [
                {
                    "key": key,
                    "before": before.get(key),
                    "after": after.get(key)
                }
                for key in normalized_keys if key in applied_key_set
            ],
            "scope": (
                "Текущий профиль и активный экструдер; "
                "без настроек отдельных моделей."
            )
        }
        if skipped:
            response["skipped"] = skipped
        return response

    def schemas(self, include_diagnostics=False):
        """
        По умолчанию предоставляет 3 целевых инструмента для мгновенной настройки за 1 шаг.
        Диагностические инструменты подключаются только если пользователь явно попросил проверить сетку/G-code или найти редкий параметр.
        """
        definitions = [
            (
                "set_material",
                "ШАГ 1: Выбрать профиль материала по его material_id из списка materials в начальном контексте. Достаточно передать только material_id.",
                {
                    "material_id": {"type": "string"},
                    "extruder_id": {"type": "string"},
                    "printer_id": {"type": "string"}
                },
                ["material_id"]
            ),
            (
                "set_print_settings",
                "ШАГ 2: Применить ВСЕ параметры печати в слайсере за один вызов (material_print_temperature, material_print_temperature_layer_0, material_bed_temperature, material_bed_temperature_layer_0, layer_height, wall_line_count, outer_inset_first, infill_sparse_density, infill_pattern, support_enable, support_tree_enable, support_roof_enable, support_top_distance, adhesion_type, speed_print, cool_fan_speed, retraction_amount, retraction_speed).",
                {
                    "values": {
                        "type": "object",
                        "additionalProperties": {
                            "anyOf": [
                                {"type": "number"},
                                {"type": "boolean"},
                                {"type": "string"}
                            ]
                        }
                    }
                },
                ["values"]
            ),
            (
                "start_slicing",
                "ШАГ 3: Запустить нарезку (слайсинг) модели для генерации G-code.",
                {},
                []
            )
        ]

        definitions.extend([
            ("list_materials", "Найти доступный профиль материала; query пустой — список, offset для продолжения.", {"query":{"type":"string"},"offset":{"type":"integer","minimum":0},"limit":{"type":"integer","minimum":1,"maximum":50}}, []),
            ("get_material_details", "Прочитать параметры штатного профиля материала по точному ID.", {"material_id":{"type":"string"}}, ["material_id"]),
            ("get_print_settings", "Прочитать действующие значения параметров.", {"keys":{"type":"array","items":{"type":"string"}}}, [])
        ])

        if include_diagnostics:
            definitions.extend([
                (
                    "validate_geometry",
                    "Проверить сетки активного стола и параметры 5D по явному запросу пользователя. Обязательно сообщи not_checked.",
                    {},
                    []
                ),
                (
                    "validate_toolpaths",
                    "Проверить имеющийся G-code по явному запросу пользователя. Обязательно сообщи not_checked.",
                    {},
                    []
                ),
                (
                    "list_print_settings",
                    "Найти редкие параметры текущего принтера, если их нет в начальном контексте.",
                    {
                        "query": {"type": "string"},
                        "offset": {"type": "integer", "minimum": 0},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 50}
                    },
                    []
                )
            ])

        definitions.extend([
            ('get_slicing_status', 'Получить статус нарезки и оценки времени/расхода из свежего G-code при завершении.', {}, []),
            ('get_scene_models', 'Модели активного стола, их ID, размеры и статус загрузки. Y — высота, X/Z — стол.', {}, []),
            ('load_model', 'Загрузить локальный STL/OBJ по абсолютному пути, указанному пользователем. Не придумывать путь. Дождаться completed.', {'path':{'type':'string'}}, ['path']),
            ('analyse_orientation', 'Анализ 26 осевых/наклонных ориентаций без изменения сцены. Контакт, нависания, геометрический proxy поддержек, ступенчатости и направление растяжения; ограничения обязательны. Направление нагрузки и критические грани только из явных данных пользователя, не угадывать.', {'model_id':{'type':'string'},'tensile_load_direction':{'type':'array','items':{'type':'number'},'minItems':3,'maxItems':3},'critical_face_indices':{'type':'array','items':{'type':'integer','minimum':0},'minItems':1,'maxItems':200}}, ['model_id']),
            ('orient_model', 'В 3D повернуть модель относительно текущего положения и опустить на стол. Y — вертикаль. Перед вызовом изучить analyse_orientation.', {'model_id':{'type':'string'}, 'axis':{'type':'string','enum':['x','y','z']}, 'degrees':{'type':'number','minimum':-360,'maximum':360}}, ['model_id','axis','degrees']),
            ('arrange_models', 'В 3D расставить все модели активного прямоугольного стола без пересечения bounding box. Зазор должен учитывать кайму и поддержки; после проверить validate_geometry.', {'margin_mm':{'type':'number','minimum':2,'maximum':100}}, [])
        ])
        result = []

        for name, description, properties, required in definitions:
            result.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                        "additionalProperties": False
                    }
                }
            })

        return result

    def call(self, name, arguments, cancel_event=None, deadline=None):
        if name == 'analyse_orientation':
            if not isinstance(arguments,dict) or set(arguments)-{'model_id','tensile_load_direction','critical_face_indices'} or not isinstance(arguments.get('model_id'),str):
                raise ValueError('Неверные аргументы анализа ориентации.')
            snapshot = self.on_main_thread(lambda: self._scene_tools.orientation_snapshot(arguments['model_id']), cancel_event, deadline)
            return analyse_orientation_mesh(snapshot, arguments.get('tensile_load_direction'), arguments.get('critical_face_indices'), cancel_event, deadline)

        if name in ("validate_geometry", "validate_toolpaths"):
            if not isinstance(arguments, dict) or arguments:
                raise ValueError("Инструмент не принимает аргументов.")
            snapshot, analyse = (snapshot_geometry, analyse_geometry) if name == "validate_geometry" else (snapshot_gcode, analyse_gcode)
            data = self.on_main_thread(snapshot, cancel_event, deadline)
            report = analyse(data, cancel_event, deadline)
            if name == 'validate_geometry':
                self._scene_tools.dirty = False
            return report
        if name == 'start_slicing':
            self.on_main_thread(self._scene_tools.ready, cancel_event, deadline)
            if self._scene_tools.dirty:
                raise ValueError('После изменения сцены сначала вызовите validate_geometry и оцените её результат.')
        allowed = {
            'get_scene_models': (self._scene_tools.get_scene_models, set()),
            'load_model': (self._scene_tools.load_model, {'path'}),
            'orient_model': (self._scene_tools.orient_model, {'model_id','axis','degrees'}),
            'arrange_models': (self._scene_tools.arrange_models, {'margin_mm'}),
            "list_materials": (self._materials.list_materials, {"query", "offset", "limit"}),
            "get_material_details": (self._materials.get_material_details, {"material_id"}),
            "set_material": (self._materials.set_material, {"material_id", "extruder_id", "printer_id"}),
            "list_print_settings": (
                self.describe,
                {"query", "offset", "limit"}
            ),
            "get_print_settings": (
                self.read,
                {"keys"}
            ),
            "set_print_settings": (
                self.write,
                {"values"}
            ),
            "start_slicing": (self.start_slicing, set()),
            "get_slicing_status": (self.get_slicing_status, set()),
            "get_print_context": (self.get_print_context, set()),
            "auto_export": (self.auto_export, set())
        }

        if name not in allowed or not isinstance(arguments, dict):
            raise ValueError(
                "Неизвестный инструмент или неверные аргументы."
            )

        function, keys = allowed[name]

        if set(arguments) - keys:
            raise ValueError(
                "Неизвестные аргументы инструмента."
            )

        result = self.on_main_thread(lambda: function(**arguments), cancel_event, deadline)
        if name == 'load_model':
            while result.get('status') == 'loading':
                if cancel_event is not None and cancel_event.wait(.15):
                    raise RuntimeError('Ожидание отменено; штатная загрузка файла может завершиться позже.')
                if cancel_event is None:
                    time.sleep(.15)
                if deadline is not None and time.monotonic() >= deadline:
                    raise RuntimeError('Истёк срок ожидания загрузки; проверьте сцену.')
                scene = self.on_main_thread(self._scene_tools.get_scene_models, cancel_event, deadline)
                result['status'] = scene['loads'].get(result['path'], 'failed')
                result['scene'] = scene
            if result.get('status') != 'completed':
                raise ValueError(result.get('status'))
            result['changes'] = [{'key':'loaded_model','before':None,'after':result['path']}]
        return result

    def initial_context(self):
        """Полный и структурированный снимок контекста, настроек и паспортов всех материалов за одно обращение к Qt."""
        global_stack, extruder = self._stacks()
        stacks = (global_stack, extruder)
        settings = []
        available = global_stack.getAllKeys()
        values_map = self.read(None)

        for key in self.MAIN_SETTINGS:
            if key not in available:
                continue
            stack = self._stack(key, stacks)
            meta = self._metadata(key, stack)
            if not self._editable(key, meta):
                continue
            compact = {
                "key": key,
                "value": values_map.get(key),
                "type": meta["type"],
                "description": str(meta.get("description", ""))[:400],
                "enabled": bool(stack.getProperty(key, "enabled"))
            }
            if "options" in meta:
                compact["options"] = list(meta["options"].keys())
            if "minimum_value" in meta or "maximum_value" in meta:
                compact["range"] = [meta.get("minimum_value"), meta.get("maximum_value")]
            settings.append(compact)

        materials_list = []
        materials_total = None
        try:
            m_res = self._materials.list_materials(limit=100)
            materials_total = m_res.get("total")
            for m in m_res.get("materials", []):
                if not m.get("selectable", True):
                    continue
                materials_list.append({
                    "material_id": m.get("material_id"),
                    "brand": m.get("brand", ""),
                    "name": m.get("name", ""),
                    "type": m.get("material", ""),
                    "nozzle_temp": m.get("default_material_print_temperature", m.get("material_print_temperature")),
                    "bed_temp": m.get("default_material_bed_temperature", m.get("material_bed_temperature")),
                    "fan_speed": m.get("cool_fan_speed"),
                    "retraction_amount": m.get("retraction_amount"),
                    "retraction_speed": m.get("retraction_speed"),
                    "density": m.get("properties_density", m.get("density", "")),
                    "description": str(m.get("description", "") or "")[:900],
                    "adhesion_info": str(m.get("adhesion_info", "") or "")[:300]
                })
        except Exception:
            pass
        return {
            "context": self.get_print_context(),
            "settings": settings,
            "materials": materials_list,
            "materials_total": materials_total,
            "materials_note": "Все доступные профили; остальные доступны через list_materials."
        }

    def get_print_context(self):
        global_stack, extruder = self._stacks()
        active = extruder if extruder is not None else global_stack

        def container_name(attribute):
            container = getattr(active, attribute, None)
            return container.getName() if container is not None else None

        app = Application.getInstance()
        scene = app.getController().getScene()
        models = []
        for node in scene.getRoot().getAllChildren():
            mesh = node.getMeshData()
            if mesh is None or not node.callDecoration("isSliceable") or node.callDecoration("getBuildPlateNumber") != app.getMultiBuildPlateModel().activeBuildPlate:
                continue
            box = node.getBoundingBox()
            has_overhangs = None
            models.append({
                "name": node.getName(),
                "size_mm": {"x": float(box.width), "y": float(box.height), "z": float(box.depth)},
                "has_critical_overhangs": has_overhangs,
                "support_required": None,
                "support_note": "Не определено: исходные нормали не доказывают необходимость поддержек.",
                "build_plate": node.callDecoration("getBuildPlateNumber")
            })
        return {
            "printer": {"id": global_stack.getId(), "name": global_stack.getName()},
            "printer_id": global_stack.getId(),
            "active_extruder": (
                {"id": extruder.getId(), "name": extruder.getName()}
                if extruder is not None else None
            ),
            "extruder_id": extruder.getId() if extruder is not None else None,
            "material": container_name("material"),
            "profile": container_name("quality"),
            "models": models
        }

    def start_slicing(self):
        self._stacks()
        return self._slice_monitor.start()

    def get_slicing_status(self):
        self._stacks()
        return self._slice_monitor.status()

    def cancel_slicing(self):
        return self.on_main_thread(self._slice_monitor.cancel)

    def auto_export(self):
        from .AutoExport import auto_export_project_and_gcode
        return auto_export_project_and_gcode()
