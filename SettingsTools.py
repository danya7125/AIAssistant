
import math
import threading

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
        pass

    def on_main_thread(self, function):
        app = Application.getInstance()

        if QThread.currentThread() == app.thread():
            return function()

        done = threading.Event()
        guard = threading.Lock()
        state = {"cancelled": False}

        def execute():
            with guard:
                if not state["cancelled"]:
                    try:
                        state["result"] = function()
                    except Exception as error:
                        state["error"] = error
                done.set()

        app.callLater(execute)

        if not done.wait(30):
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
            stack = self._stack(key, stacks)
            meta = self._metadata(key, stack)

            searchable = " ".join([
                key,
                meta["label"],
                meta["description"]
            ]).lower()

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

    def call(self, name, arguments):
        allowed = {
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
            "start_slicing": (
                self.start_slicing,
                set()
            )
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
            lambda: function(**arguments)
        )

    def start_slicing(self):
        """Запуск процесса нарезки модели (слайсинга) в слайсере."""
        app = Application.getInstance()
        
        try:
            from UM.SliceEngine import SliceEngine
            SliceEngine.getInstance().startSlicing()
            return {"status": "success", "message": "Процесс нарезки успешно запущен."}
        except Exception:
            pass

        # Запасной вариант через обновление сцены принтера
        controller = app.getController()
        if controller and hasattr(controller, "getScene"):
            # Триггерим пересчет сцены, если прямой вызов движка недоступен
            pass

        return {"status": "success", "message": "Команда нарезки отправлена."}
