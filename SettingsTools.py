
import math
import threading
import time
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
        "wall_thickness",
        "wall_line_count",
        "top_layers",
        "bottom_layers",
        "material_print_temperature",
        "material_bed_temperature",
        "speed_print",
        "speed_travel",
        "support_enable",
        "support_type",
        "support_pattern",
        "adhesion_type",
        "cool_fan_enabled",
        "cool_fan_speed",
        "retraction_enable",
        "retraction_amount",
        "retraction_speed",
        "cylindrical_mode_base_diameter",
        "non_printing_base_diameter",
        "cylindrical_round_segments"
    ]

    EDITABLE_TYPES = ("bool", "int", "float", "enum")
    PRINTING_MODES = ("classic", "cylindrical", "cylindrical_full")

    def __init__(self):
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

        if keys is None:
            keys = self.MAIN_SETTINGS

        if (
            not isinstance(keys, (list, tuple))
            or any(not isinstance(k, str) for k in keys)
        ):
            raise ValueError(
                "keys должен быть списком названий параметров."
            )

        result = {}

        for key in keys:
            stack = self._stack(key, stacks)
            value = stack.getProperty(key, "value")

            if value is not None and not isinstance(
                value, (str, int, float, bool)
            ):
                value = str(value)

            result[key] = value

        return result

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
            # Only static search text is cached; values/bounds remain live.
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
        meta = self._metadata(key, stack)

        if not self._editable(key, meta):
            raise ValueError(
                "Параметр доступен только для чтения: " + key
            )

        kind = meta["type"]

        if kind == "bool":
            if type(value) is not bool:
                raise ValueError(
                    key + ": требуется true или false."
                )

        elif kind in ("int", "float"):
            if type(value) not in (int, float):
                raise ValueError(
                    key + ": требуется число."
                )

            if not math.isfinite(value):
                raise ValueError(
                    key + ": число должно быть конечным."
                )

            if kind == "int":
                if int(value) != value:
                    raise ValueError(
                        key + ": требуется целое число."
                    )
                value = int(value)

            minimum = meta.get("minimum_value")
            maximum = meta.get("maximum_value")

            if minimum is not None and value < minimum:
                raise ValueError(
                    "{}: значение меньше минимума {}.".format(
                        key, minimum
                    )
                )

            if maximum is not None and value > maximum:
                raise ValueError(
                    "{}: значение больше максимума {}.".format(
                        key, maximum
                    )
                )

        elif kind == "enum":
            options = meta.get("options", {})

            if not isinstance(value, str) or value not in options:
                raise ValueError(
                    key + ": доступные варианты: "
                    + ", ".join(options)
                )

            if (
                key == "printing_mode"
                and value not in self.PRINTING_MODES
            ):
                raise ValueError(
                    "Разрешены только 3D, 5D Spiral и 5D."
                )

        return value

    def write(self, values):
        if (
            not isinstance(values, dict)
            or not values
            or len(values) > 25
        ):
            raise ValueError(
                "values должен содержать от 1 до 25 параметров."
            )

        if any(not isinstance(k, str) for k in values):
            raise ValueError(
                "Названия параметров должны быть строками."
            )

        stacks = self._stacks()
        prepared = []

        for key, value in values.items():
            stack = self._stack(key, stacks)
            value = self._validate(key, value, stack)

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

        before = self.read(list(values))
        applied = []

        try:
            for item in prepared:
                key, value, stack, container, existed, old = item

                applied.append((
                    key, container, existed, old
                ))

                container.setProperty(key, "value", value)

            # Re-check after all updates.
            for item in prepared:
                key, value, stack, container, existed, old = item

                self._validate(key, value, stack)

                if stack.getProperty(key, "enabled") is False:
                    raise ValueError(
                        "Параметр неактивен: " + key
                    )

                state = str(
                    stack.getProperty(key, "validationState")
                )

                if "Error" in state or "Exception" in state:
                    raise ValueError(
                        "Слайсер отклонил {}: {}".format(
                            key, state
                        )
                    )

            after = self.read(list(values))

            for key, value, stack, container, existed, old in prepared:
                if after[key] != value:
                    raise ValueError(
                        "Слайсер не применил значение: " + key
                    )

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

        return {
            "changes": [
                {
                    "key": key,
                    "before": before[key],
                    "after": after[key]
                }
                for key in values
            ],
            "scope": (
                "Текущий профиль и активный экструдер; "
                "без настроек отдельных моделей."
            )
        }

    def schemas(self):
        definitions = [
            (
                "list_print_settings",
                "Найти реальные параметры текущего принтера. "
                "Возвращает типы, значения, варианты и границы.",
                {
                    "query": {"type": "string"},
                    "offset": {
                        "type": "integer",
                        "minimum": 0
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 50
                    }
                },
                []
            ),
            (
                "get_print_settings",
                "Прочитать актуальные настройки печати.",
                {
                    "keys": {
                        "type": "array",
                        "items": {"type": "string"}
                    }
                },
                []
            ),
            (
                "set_print_settings",
                "Изменить параметры по запросу пользователя. "
                "Сначала проверь типы и границы через "
                "list_print_settings. Не изменяй параметры "
                "без запроса пользователя.",
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
                "Запустить процесс нарезки (слайсинга) модели для генерации G-code.",
                {},
                []
            )
        ]

        definitions.extend([
            ("list_materials", "Список существующих профилей для текущего принтера и активного экструдера. Поиск по названию/типу/бренду, например ASA. Возвращает идентификаторы и описание, если оно есть. Это не проверка свойств по ТЗ.",
             {"query": {"type": "string"}, "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 50}}, []),
            ("set_material", "Выбрать профиль из list_materials по явной команде пользователя. Передай все три ID из списка. Прочитай настройки заново после переключения; при requires_user_action остановись.",
             {"material_id": {"type": "string"}, "extruder_id": {"type": "string"}, "printer_id": {"type": "string"}}, ["material_id", "extruder_id", "printer_id"]),
            ("get_slicing_status", "Проверить фактическое завершение нарезки и наличие G-code. completed не означает сохранение файла.", {}, []),
            ("get_print_context", "Текущий принтер, материал, профиль и размеры загруженных моделей. Не подтверждает физические характеристики материала.", {}, [])
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
        allowed = {
            "list_materials": (self._materials.list_materials, {"query", "offset", "limit"}),
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
            "get_print_context": (self.get_print_context, set())
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

        return self.on_main_thread(
            lambda: function(**arguments), cancel_event, deadline
        )

    def initial_context(self):
        """One Qt dispatch supplies context and exact primary setting metadata."""
        global_stack, extruder = self._stacks()
        stacks = (global_stack, extruder)
        settings = []
        available = global_stack.getAllKeys()
        for key in self.MAIN_SETTINGS:
            if key not in available:
                continue
            stack = self._stack(key, stacks)
            meta = self._metadata(key, stack)
            meta.pop("description", None)
            meta["value"] = self.read([key])[key]
            meta["editable"] = self._editable(key, meta)
            meta["enabled"] = bool(stack.getProperty(key, "enabled"))
            settings.append(meta)
        return {"context": self.get_print_context(), "settings": settings}

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
            if node.getMeshData() is None or not node.callDecoration("isSliceable"):
                continue
            box = node.getBoundingBox()
            models.append({"name": node.getName(),
                           "size_mm": {"x": float(box.width), "y": float(box.height), "z": float(box.depth)},
                           "build_plate": node.callDecoration("getBuildPlateNumber")})
        return {"printer": global_stack.getName(), "material": container_name("material"),
                "profile": container_name("quality"), "models": models,
                "notes": "Название материала не подтверждает его свойства. Tg, прочность и химстойкость нужны из документации производителя. Размеры модели не заменяют расчёт прочности."}

    def start_slicing(self):
        self._stacks()
        return self._slice_monitor.start()

    def get_slicing_status(self):
        self._stacks()
        return self._slice_monitor.status()

    def cancel_slicing(self):
        return self.on_main_thread(self._slice_monitor.cancel)
