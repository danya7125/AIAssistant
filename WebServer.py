import json
import os
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, unquote, urlsplit

plugin_web = os.path.abspath(os.path.join(os.path.dirname(__file__), "web"))
if os.path.isdir(plugin_web):
    WEB_DIR = plugin_web
else:
    WEB_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, "web"))


class ChatServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(SimpleHTTPRequestHandler):

    def translate_path(self, path):
        path = unquote(urlsplit(path).path)
        parts = [
            part for part in path.split("/")
            if part not in ("", ".", "..")
            and "\\" not in part
            and ":" not in part
        ]
        return os.path.join(WEB_DIR, *parts)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        SimpleHTTPRequestHandler.end_headers(self)

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

    def do_GET(self):
        path = urlsplit(self.path).path
        assistant = self.server.assistant

        if path == "/api/history":
            self.send_json(200, {
                "messages": assistant.get_web_history(),
                "event_cursor": assistant.get_web_event_cursor()
            })
            return

        if path == "/api/events":
            query = parse_qs(urlsplit(self.path).query)
            try:
                since = int(query.get("since", ["0"])[0])
            except (TypeError, ValueError):
                self.send_json(400, {"error": "Некорректный курсор событий."})
                return
            if since < 0:
                self.send_json(400, {"error": "Некорректный курсор событий."})
                return
            events, cursor = assistant.get_web_events(since)
            self.send_json(200, {"events": events, "next_id": cursor})
            return

        if path == "/api/open-folder":
            try:
                from PyQt5.QtCore import QUrl
                from PyQt5.QtGui import QDesktopServices
                from .AutoExport import get_output_base_dir
                target_dir = getattr(assistant, "_latest_export_dir", None)
                if not target_dir or not os.path.exists(target_dir):
                    target_dir = get_output_base_dir()
                if os.path.exists(target_dir):
                    QDesktopServices.openUrl(QUrl.fromLocalFile(target_dir))
                    self.send_json(200, {"ok": True, "dir": target_dir})
                else:
                    self.send_json(404, {"error": "Папка не найдена"})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path.startswith("/api/"):
            self.send_json(404, {"error": "Неизвестный адрес."})
            return

        SimpleHTTPRequestHandler.do_GET(self)

    def do_POST(self):
        if urlsplit(self.path).path != "/api/message":
            self.send_json(404, {"error": "Неизвестный адрес."})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            self.send_json(400, {"error": "Некорректный размер запроса."})
            return

        if length <= 0 or length > 100000:
            self.send_json(400, {"error": "Недопустимый размер запроса."})
            return

        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self.send_json(400, {"error": "Ожидается корректный JSON."})
            return

        if not isinstance(data, dict):
            self.send_json(400, {"error": "Ожидается JSON-объект."})
            return

        text = data.get("text")
        if not isinstance(text, str) or not text.strip():
            self.send_json(400, {"error": "Введите сообщение."})
            return
        if len(text) > 24000:
            self.send_json(400, {"error": "Сократите сообщение до 24000 символов."})
            return

        try:
            reply = self.server.assistant.ask_from_web(text.strip())
        except RuntimeError as error:
            self.send_json(409, {"error": str(error)})
            return
        except Exception as error:
            self.log_error("Web chat request failed: %s", error)
            self.send_json(500, {
                "error": "Не удалось обработать запрос. Проверьте журнал STE Slicer."
            })
            return

        self.send_json(200, {"reply": reply})
