import json
import os
import tempfile
import uuid
from email import policy
from email.parser import BytesParser
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, unquote, urlsplit


MAX_REQUEST_BYTES = 50 * 1024 * 1024
MAX_ATTACHMENTS = 8
ALLOWED_MODEL_EXTENSIONS = {".stl"}


plugin_web = os.path.abspath(os.path.join(os.path.dirname(__file__), "web"))
if os.path.isdir(plugin_web):
    WEB_DIR = plugin_web
else:
    WEB_DIR = os.path.abspath(
        os.path.join(os.path.dirname(__file__), os.pardir, "web")
    )


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
        if not parts:
            return os.path.join(WEB_DIR, "index.html")
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

        if path == "/api/profile":
            try:
                profile = assistant.get_web_profile()
            except Exception as error:
                self.log_error("Could not read active print profile: %s", error)
                self.send_json(500, {"error": "Не удалось прочитать профиль слайсера."})
                return
            self.send_json(200, profile)
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

        if length <= 0 or length > MAX_REQUEST_BYTES:
            self.send_json(400, {"error": "Размер сообщения или вложений превышает 50 МБ."})
            return

        try:
            body = self.rfile.read(length)
            content_type = self.headers.get("Content-Type", "")
            if content_type.lower().startswith("multipart/form-data"):
                text, attachments = self._parse_multipart(content_type, body)
            elif content_type.lower().startswith("application/json"):
                data = json.loads(body.decode("utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("Ожидается JSON-объект.")
                text = data.get("text", "")
                attachments = []
            else:
                raise ValueError("Неподдерживаемый формат запроса.")
        except (UnicodeDecodeError, ValueError) as error:
            self.send_json(400, {"error": str(error) or "Некорректный запрос."})
            return

        if not isinstance(text, str) or (not text.strip() and not attachments):
            self.send_json(400, {"error": "Введите сообщение."})
            return
        if len(text) > 24000:
            self.send_json(400, {"error": "Сократите сообщение до 24000 символов."})
            return

        try:
            if attachments:
                with tempfile.TemporaryDirectory(prefix="ste-slicer-ai-") as upload_dir:
                    uploaded_files = []
                    for attachment in attachments:
                        extension = os.path.splitext(attachment["name"])[1].lower()
                        saved_path = os.path.join(upload_dir, uuid.uuid4().hex + extension)
                        with open(saved_path, "wb") as uploaded_file:
                            uploaded_file.write(attachment["content"])
                        uploaded_files.append({
                            "name": attachment["name"],
                            "path": saved_path
                        })
                    reply = self.server.assistant.ask_from_web(
                        text.strip(),
                        attachments=uploaded_files
                    )
            else:
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

    @staticmethod
    def _parse_multipart(content_type, body):
        mime_message = BytesParser(policy=policy.default).parsebytes(
            ("Content-Type: " + content_type + "\r\n"
             "MIME-Version: 1.0\r\n\r\n").encode("ascii") + body
        )
        if not mime_message.is_multipart():
            raise ValueError("Не удалось прочитать вложения.")

        text = ""
        attachments = []
        for part in mime_message.iter_parts():
            field_name = part.get_param("name", header="content-disposition")
            filename = part.get_filename()
            if field_name == "text" and filename is None:
                text = part.get_content()
                continue
            if field_name != "attachments" or filename is None:
                raise ValueError("Неизвестное поле формы.")

            if len(attachments) >= MAX_ATTACHMENTS:
                raise ValueError("Можно прикрепить не более 8 моделей.")

            safe_name = filename.replace("\\", "/").split("/")[-1]
            safe_name = "".join(
                character for character in safe_name
                if character >= " " and character not in "\r\n"
            ).strip()
            extension = os.path.splitext(safe_name)[1].lower()
            if not safe_name or extension not in ALLOWED_MODEL_EXTENSIONS:
                raise ValueError("Поддерживаются только файлы STL.")

            content = part.get_payload(decode=True)
            if not content:
                raise ValueError("Файл пуст: " + safe_name)
            attachments.append({"name": safe_name[:255], "content": content})

        return text, attachments
