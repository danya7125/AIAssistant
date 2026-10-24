import json
import os
import tempfile
import uuid
from .ProfileCard import parse_card
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


def setting_schema():
    return {"type": "function", "function": {
        "name": "set_setting",
        "description": "Изменить один параметр печати в активном профиле/экструдере. Настоящий key из list_print_settings. Материал выбирать через set_material. printing_mode и аппаратные параметры запрещены.",
        "parameters": {"type": "object", "properties": {
            "key": {"type": "string", "description": "Например material_print_temperature, infill_sparse_density, speed_print, layer_height"},
            "value": {"type": "string", "description": "Значение без единиц: 260, 0.2, true; для enum точный ключ варианта"}
        }, "required": ["key", "value"], "additionalProperties": False}}}


def dispatch_setting(settings_tools, arguments, cancel_event=None, deadline=None):
    """Internal LLM dispatcher, not an unrestricted public HTTP write endpoint."""
    if not isinstance(arguments, dict) or set(arguments) != {'key', 'value'}:
        raise ValueError('set_setting требует ровно key и value.')
    key, value = arguments['key'], arguments['value']
    if not isinstance(key, str) or not key or len(key) > 200:
        raise ValueError('key должен быть настоящим именем параметра слайсера.')
    if key == 'printing_mode' or key.startswith('machine_'):
        raise ValueError('Режим печати и аппаратные параметры меняются только вручную.')
    if key in ('material', 'material_id'):
        raise ValueError('Для выбора профиля материала используйте set_material(material_id=...).')
    if not isinstance(value, str) or len(value) > 1000:
        raise ValueError('value должен быть строкой без единиц измерения.')
    # on_main_thread schedules app.callLater(execute), waits for completion,
    # and cancels the queued callback if the request expires before execution.
    # write selects the correct global/extruder stack, validates types/ranges,
    # reads the actual value back, and rolls back if validation fails.
    result = settings_tools.on_main_thread(
        lambda: settings_tools.write({key: value}), cancel_event=cancel_event, deadline=deadline)
    if not isinstance(result, dict) or result.get('skipped'):
        raise ValueError('Слайсер не подтвердил изменение параметра.')
    change = next((c for c in result.get('changes', []) if c.get('key') == key), None)
    if change is None:
        raise ValueError('Слайсер не вернул проверенное значение параметра ' + key)
    result = dict(result)
    result['status'] = 'success'
    result['message'] = 'Параметр {0} установлен: {1}'.format(key, change['after'])
    return result


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

        if path.startswith('/api/') and not self._local_request():
            self.send_json(403, {"error": "Запрос разрешён только из локального интерфейса."})
            return

        if path == "/api/model_stl":
            query = parse_qs(urlsplit(self.path).query)
            model_path = query.get("path", ["FROM_SCENE"])[0]
            try:
                body = assistant.get_web_model_stl(model_path)
            except (ValueError, OSError, RuntimeError) as error:
                self.send_json(400, {"error": str(error)})
                return
            except Exception:
                self.send_json(500, {"error": "Не удалось экспортировать модель из сцены."})
                return
            self.send_response(200)
            self.send_header("Content-Type", "model/stl")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Model-Up-Axis", "Y" if model_path == "FROM_SCENE" else "Z")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

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
        if not self._local_request():
            self.send_json(403, {"error": "Запрос разрешён только из локального интерфейса."})
            return
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
                        self.server.assistant.register_web_model(saved_path, attachment["content"])
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

        # Preserve the original text for legacy clients; structured data for the web UI.
        card = parse_card(reply)
        self.send_json(200, {"reply": reply, "card": card})

    def _local_request(self):
        # No CORS; also reject DNS rebinding and foreign-page form submissions.
        host = self.headers.get('Host', '')
        allowed = {'127.0.0.1:' + str(self.server.server_port),
                   'localhost:' + str(self.server.server_port)}
        origin = self.headers.get('Origin')
        return host in allowed and (origin is None or origin == 'http://' + host)

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
                raw_bytes = part.get_payload(decode=True)
                text = raw_bytes.decode('utf-8') if raw_bytes else ""
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
