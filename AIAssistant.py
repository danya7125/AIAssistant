
import threading
import webbrowser

from .ChatWindow import ChatWindow
from .SettingsTools import SettingsTools
from .AgentClient import AgentClient
from .WebServer import ChatServer, Handler

from PyQt5.QtCore import QThread
from PyQt5.QtWidgets import (
    QDialog,
    QFormLayout,
    QLineEdit,
    QMessageBox,
    QPushButton,
)

from UM.Application import Application
from UM.Extension import Extension


# Основной класс плагина STE Slicer
class AIAssistant(Extension):

    def __init__(self):
        super().__init__()

        # Пункты меню плагина
        self.setMenuName("AI Assistant")
        self.addMenuItem("Settings", self.showSettings)
        self.addMenuItem("Open chat", self.showChat)
        self.addMenuItem("Open web chat", self.showWebChat)

        self._settings_tools = SettingsTools()
        self._agent = AgentClient(self._settings_tools)
        self._server = None
        self._lock = threading.Lock()
        self._history = []
        self._web_events = []
        self._web_event_id = 0
        self._web_event_lock = threading.Lock()
        self._web_request_lock = threading.Lock()
        self._latest_export_dir = None
        self._web_models = {}
        self._web_model_lock = threading.Lock()

        self._preferences = (
            Application.getInstance().getPreferences()
        )

        defaults = {
            "url": "https://api.timeweb.ai/v1",
            "key": "",
            "model": ""
        }

        for name, value in defaults.items():
            self._preferences.addPreference(
                "ai_assistant/" + name,
                value
            )

        self._config = self.read_config()

    # Чтение конфигурации LLM
    def read_config(self):
        return {
            name: str(
                self._preferences.getValue(
                    "ai_assistant/" + name
                ) or ""
            ).strip()
            for name in ("url", "key", "model")
        }

    # Получение настроек печати
    def get_print_settings(self, keys=None):
        """Read on the main Qt thread. None selects the 25 main settings."""
        return self._settings_tools.read(keys)

    def set_print_settings(self, values):
        """Apply validated values on the main Qt thread and read them back."""
        return self._settings_tools.write(values)

    # Окно настройки LLM
    def showSettings(self):

        existing = getattr(
            self,
            "_settings_dialog",
            None
        )

        if existing is not None:
            existing.show()
            existing.raise_()
            existing.activateWindow()
            return

        dialog = QDialog()
        dialog.setWindowTitle(
            "AI Assistant - Settings"
        )
        dialog.resize(480, 200)

        layout = QFormLayout(dialog)
        current = self.read_config()

        url_input = QLineEdit(dialog)
        url_input.setText(current["url"])
        layout.addRow("API URL:", url_input)

        key_input = QLineEdit(dialog)
        key_input.setEchoMode(QLineEdit.Password)
        key_input.setText(current["key"])
        layout.addRow("API key:", key_input)

        model_input = QLineEdit(dialog)
        model_input.setText(current["model"])
        layout.addRow("Model:", model_input)

        save_button = QPushButton(
            "Сохранить",
            dialog
        )
        layout.addRow(save_button)

        def save():
            updated = {
                "url": url_input.text().strip(),
                "key": key_input.text().strip(),
                "model": model_input.text().strip()
            }

            for name, value in updated.items():
                self._preferences.setValue(
                    "ai_assistant/" + name,
                    value
                )

            self._config = updated
            dialog.close()

        def finished(result):
            self._settings_dialog = None
            dialog.deleteLater()

        save_button.clicked.connect(save)
        dialog.finished.connect(finished)

        self._settings_dialog = dialog

        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        key_input.setFocus()

    # Открытие чата Qt
    def showChat(self):

        window = getattr(
            self,
            "_chat_window",
            None
        )

        if window is None:
            window = ChatWindow(self)
            self._chat_window = window

        window.showNormal()
        window.raise_()
        window.activateWindow()
        window.input.setFocus()

    # Открытие веб-чата в браузере
    def showWebChat(self):
        try:
            if self._server is None:
                self._server = ChatServer(
                    ("127.0.0.1", 0),
                    Handler
                )
                self._server.assistant = self
                worker = threading.Thread(
                    target=self._server.serve_forever
                )
                worker.daemon = True
                worker.start()

            url = "http://127.0.0.1:{0}/".format(
                self._server.server_address[1]
            )
            if not webbrowser.open(url):
                QMessageBox.warning(
                    None,
                    "AI Assistant",
                    "Не удалось открыть браузер. Адрес чата: " + url
                )
        except OSError as error:
            QMessageBox.warning(
                None,
                "AI Assistant",
                "Не удалось запустить веб-чат: " + str(error)
            )

    def get_web_history(self):
        return [
            {"role": item["role"], "content": item["content"]}
            for item in list(self._history)
            if item.get("role") in ("user", "assistant")
            and isinstance(item.get("content"), str)
        ]

    def get_web_event_cursor(self):
        with self._web_event_lock:
            return self._web_event_id

    def get_web_profile(self):
        return self._settings_tools.on_main_thread(
            self._settings_tools.profile_snapshot
        )

    def register_web_model(self, path, data):
        # Keep recent uploads available after TemporaryDirectory is removed.
        import os
        with self._web_model_lock:
            if sum(len(v) for v in self._web_models.values()) + len(data) > 50 * 1024 * 1024:
                self._web_models.clear()
            self._web_models[os.path.normcase(os.path.realpath(path))] = data

    def get_web_model_stl(self, path):
        import os
        from .ProfileCard import scene_stl
        if path == "FROM_SCENE":
            return self._settings_tools.on_main_thread(
                lambda: scene_stl(self._settings_tools._scene_tools))
        if (not isinstance(path, str) or len(path) > 4096 or not os.path.isabs(path)
                or path.startswith(("\\\\", "//")) or os.path.splitext(path)[1].lower() != ".stl"):
            raise ValueError("Нужен локальный STL из вложений или текущей сцены.")
        canonical = os.path.normcase(os.path.realpath(path))
        with self._web_model_lock:
            cached = self._web_models.get(canonical)
        if cached is not None:
            return cached
        # A model-generated path cannot read arbitrary files from the computer.
        allowed = self._settings_tools.on_main_thread(lambda: [
            os.path.normcase(os.path.realpath(n.getMeshData().getFileName()))
            for n in self._settings_tools._scene_tools.nodes()
            if n.getMeshData().getFileName()])
        if canonical not in allowed:
            raise ValueError("Этот STL не прикреплён к чату и не загружен в сцену.")
        with open(canonical, "rb") as source:
            data = source.read(50 * 1024 * 1024 + 1)
        if not data or len(data) > 50 * 1024 * 1024:
            raise ValueError("STL пуст или превышает 50 МБ.")
        return data

    def get_web_events(self, since):
        with self._web_event_lock:
            events = [
                item for item in self._web_events
                if item["id"] > since
            ]
            return events, self._web_event_id

    def _record_web_event(self, event):
        with self._web_event_lock:
            self._web_event_id += 1
            self._web_events.append({
                "id": self._web_event_id,
                "event": event
            })
            if len(self._web_events) > 500:
                del self._web_events[:-500]

    def ask_from_web(self, text, attachments=None):
        if not self._web_request_lock.acquire(False):
            raise RuntimeError("Веб-чат уже обрабатывает сообщение.")

        try:
            self._record_web_event({
                "phase": "status",
                "message": "Ожидаем ответ…"
            })
            settings = self._settings_tools.on_main_thread(
                self.get_print_settings
            )
            request_text = text
            history_text = text
            if attachments:
                attachment_lines = [
                    "- {0}: {1}".format(item["name"], item["path"])
                    for item in attachments
                ]
                request_text += (
                    "\n\nВложения, временно сохранённые локально для этого запроса:\n"
                    + "\n".join(attachment_lines)
                    + "\nИспользуй путь только если пользователь явно попросил загрузить "
                    "или открыть соответствующую модель. Не загружай её в сцену сам."
                )
                history_text += (
                    "\nПрикреплены модели: "
                    + ", ".join(item["name"] for item in attachments)
                )
            return self.ask_llm(
                request_text,
                print_settings=settings,
                history_text=history_text
            )
        finally:
            self._web_request_lock.release()

    # Отправка запроса к нейросети
    def ask_llm(self, text, print_settings=None, progress=None, cancel_event=None, task_text=None, history_text=None):
        config = self._config.copy()
        if not all(config.values()):
            return "Откройте AI Assistant → Settings и заполните URL, ключ и модель."
        if not config["url"].startswith("https://"):
            return "API URL должен начинаться с https://"
        if not self._lock.acquire(False):
            return "Предыдущий запрос ещё выполняется. Дождитесь ответа."
        try:
            def report_progress(event):
                self._record_web_event(event)
                if event and isinstance(event, dict) and event.get("name") == "auto_export":
                    out = event.get("output", {})
                    if out.get("ok"):
                        target_dir = out.get("result", {}).get("target_dir")
                        if target_dir:
                            self._latest_export_dir = target_dir
                if progress is not None:
                    progress(event)

            reply = self._agent.run(
                config, text, self._history,
                print_settings=print_settings, progress=report_progress, cancel_event=cancel_event, task_text=task_text
            )
            self._history.extend([
                {"role": "user", "content": history_text if history_text is not None else text},
                {"role": "assistant", "content": reply}
            ])
            self._history = self._history[-20:]
            return reply
        finally:
            self._lock.release()
