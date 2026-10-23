
import ssl
import json
import os
import threading

from .ChatWindow import ChatWindow
from .SettingsTools import SettingsTools
from .AgentClient import AgentClient

from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from PyQt5.QtCore import QThread
from PyQt5.QtWidgets import (
    QDialog,
    QFormLayout,
    QLineEdit,
    QPushButton,
    QPlainTextEdit,
)

from UM.Application import Application
from UM.Extension import Extension


WEB_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "web"
)


# HTTP-сервер для возможного веб-интерфейса
class ChatServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class Handler(SimpleHTTPRequestHandler):

    def translate_path(self, path):
        # Разрешаем доступ только к файлам web
        path = unquote(urlsplit(path).path)

        parts = [
            part
            for part in path.split("/")
            if part not in ("", ".", "..")
            and "\\" not in part
            and ":" not in part
        ]

        return os.path.join(WEB_DIR, *parts)

    def send_json(self, status, data):
        body = json.dumps(
            data,
            ensure_ascii=False
        ).encode("utf-8")

        self.send_response(status)
        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8"
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()

        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        if self.path != "/api/message":
            self.send_json(
                404,
                {"reply": "Неизвестный адрес"}
            )
            return

        try:
            length = int(
                self.headers.get("Content-Length", "0")
            )

            if length <= 0 or length > 100000:
                self.send_json(
                    400,
                    {"reply": "Недопустимый размер запроса"}
                )
                return

            data = json.loads(
                self.rfile.read(length).decode("utf-8")
            )

            if not isinstance(data, dict):
                self.send_json(
                    400,
                    {"reply": "Ожидается JSON-объект"}
                )
                return

            text = data.get("text", "")

            if not isinstance(text, str) or not text.strip():
                self.send_json(
                    400,
                    {"reply": "Введите сообщение"}
                )
                return

            # Веб-обработчик не обращается к Qt напрямую
            reply = self.server.assistant.ask_llm(
                text.strip()
            )

            self.send_json(
                200,
                {"reply": reply}
            )

        except Exception:
            self.send_json(
                500,
                {
                    "reply": (
                        "Ошибка обработки запроса. "
                        "Проверьте настройки LLM."
                    )
                }
            )


# Основной класс плагина STE Slicer
class AIAssistant(Extension):

    def __init__(self):
        super().__init__()

        # Пункты меню плагина
        self.setMenuName("AI Assistant")
        self.addMenuItem("Settings", self.showSettings)
        self.addMenuItem("Open chat", self.showChat)

        self._settings_tools = SettingsTools()
        self._agent = AgentClient(self._settings_tools)
        self._server = None
        self._lock = threading.Lock()
        self._history = []

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

    # Открытие чата
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

    # Отправка запроса к нейросети
    def ask_llm(self, text, print_settings=None, progress=None, cancel_event=None, task_text=None):
        config = self._config.copy()
        if not all(config.values()):
            return "Откройте AI Assistant → Settings и заполните URL, ключ и модель."
        if not config["url"].startswith("https://"):
            return "API URL должен начинаться с https://"
        if not self._lock.acquire(False):
            return "Предыдущий запрос ещё выполняется. Дождитесь ответа."
        try:
            reply = self._agent.run(
                config, text, self._history,
                print_settings=print_settings, progress=progress, cancel_event=cancel_event, task_text=task_text
            )
            self._history.extend([
                {"role": "user", "content": text},
                {"role": "assistant", "content": reply}
            ])
            self._history = self._history[-20:]
            return reply
        finally:
            self._lock.release()
