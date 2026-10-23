import json
import os
import threading
from .ChatWindow import ChatWindow

from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from PyQt5.QtWidgets import QDialog, QFormLayout, QLineEdit, QPushButton
from UM.Application import Application
from UM.Extension import Extension


WEB_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "web"
)


class ChatServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class Handler(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        # Раздаём только файлы из папки web.
        from urllib.parse import unquote, urlsplit

        path = unquote(urlsplit(path).path)
        parts = [
            part for part in path.split("/")
            if part not in ("", ".", "..")
            and "\\" not in part
            and ":" not in part
        ]
        return os.path.join(WEB_DIR, *parts)

    def send_json(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header(
            "Content-Type", "application/json; charset=utf-8"
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        if self.path != "/api/message":
            self.send_json(404, {"reply": "Неизвестный адрес"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 100000:
                self.send_json(400, {"reply": "Недопустимый размер запроса"})
                return

            data = json.loads(self.rfile.read(length).decode("utf-8"))
            text = data.get("text", "")
            if not isinstance(text, str) or not text.strip():
                self.send_json(400, {"reply": "Введите сообщение"})
                return

            reply = self.server.assistant.ask_llm(text.strip())
            self.send_json(200, {"reply": reply})
        except Exception:
            self.send_json(200, {
                "reply": "Ошибка обработки запроса. Проверьте настройки LLM."
            })


class AIAssistant(Extension):
    def __init__(self):
        super().__init__()

        self.setMenuName("AI Assistant")
        self.addMenuItem("Settings", self.showSettings)
        self.addMenuItem("Open chat", self.showChat)

        self._server = None
        self._lock = threading.Lock()
        self._history = []
        self._preferences = Application.getInstance().getPreferences()

        defaults = {
            "url": "https://api.timeweb.ai/v1",
            "key": "",
            "model": ""
        }
        for name, value in defaults.items():
            self._preferences.addPreference(
                "ai_assistant/" + name, value
            )

        self._config = self.read_config()

    def read_config(self):
        return {
            name: str(
                self._preferences.getValue("ai_assistant/" + name) or ""
            ).strip()
            for name in ("url", "key", "model")
        }

    def showSettings(self):
        existing = getattr(self, "_settings_dialog", None)
        if existing is not None:
            existing.show()
            existing.raise_()
            existing.activateWindow()
            return

        dialog = QDialog()
        dialog.setWindowTitle("AI Assistant - Settings")
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

        save_button = QPushButton("Сохранить", dialog)
        layout.addRow(save_button)

        def save():
            updated = {
                "url": url_input.text().strip(),
                "key": key_input.text().strip(),
                "model": model_input.text().strip()
            }
            for name, value in updated.items():
                self._preferences.setValue("ai_assistant/" + name, value)
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

    def showChat(self):
        window = getattr(self, "_chat_window", None)
        if window is None:
            window = ChatWindow(self)
            self._chat_window = window
        window.showNormal()
        window.raise_()
        window.activateWindow()
        window.input.setFocus()

    def ask_llm(self, text):
        config = self._config.copy()

        if not all(config.values()):
            return (
                "Откройте AI Assistant → Settings "
                "и заполните URL, API key и название модели."
            )

        if not config["url"].startswith("https://"):
            return "API URL должен начинаться с https://"

        if not self._lock.acquire(False):
            return "Предыдущий запрос ещё выполняется. Дождитесь ответа."

        try:
            messages = [{
                "role": "system",
                "content": (
                    "Ты помощник по STE Slicer и 3D/5D-печати. "
                    "Отвечай по-русски. Сейчас у тебя нет инструментов "
                    "управления слайсером. Не утверждай, что изменил "
                    "настройки, загрузил модель или запустил нарезку."
                )
            }]
            messages.extend(self._history[-20:])
            messages.append({"role": "user", "content": text})

            payload = json.dumps({
                "model": config["model"],
                "messages": messages
            }).encode("utf-8")

            request = Request(
                config["url"].rstrip("/") + "/chat/completions",
                data=payload,
                headers={
                    "Authorization": "Bearer " + config["key"],
                    "Content-Type": "application/json"
                },
                method="POST"
            )

            try:
                with urlopen(request, timeout=60) as response:
                    result = json.loads(
                        response.read().decode("utf-8")
                    )

                reply = result["choices"][0]["message"]["content"]
                if not isinstance(reply, str) or not reply.strip():
                    return "API вернул пустой текстовый ответ."

            except HTTPError as error:
                hints = {
                    400: "Проверьте название модели и параметры API.",
                    401: "Проверьте API-ключ.",
                    403: "У ключа нет доступа к этому ресурсу.",
                    404: "Проверьте API URL и название модели.",
                    429: "Превышен лимит запросов или квота."
                }
                return "Ошибка API %s. %s" % (
                    error.code,
                    hints.get(error.code, "Попробуйте повторить позже.")
                )
            except URLError as error:
                reason = str(error.reason)
                if config["key"]:
                    reason = reason.replace(config["key"], "[hidden]")
                return "Ошибка подключения к API: " + reason
            except Exception:
                return "Не удалось получить ответ LLM: тайм-аут или неожиданный формат ответа."

            self._history.extend([
                {"role": "user", "content": text},
                {"role": "assistant", "content": reply}
            ])
            self._history = self._history[-20:]
            return reply

        finally:
            self._lock.release()