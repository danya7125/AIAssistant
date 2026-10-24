
import os
import threading
import json

from PyQt5.QtCore import QObject, Qt, pyqtSignal, QUrl
from PyQt5.QtGui import QDesktopServices

from PyQt5.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTextBrowser,
    QVBoxLayout
)


# Передача результата между потоками
class ReplyBridge(QObject):
    received = pyqtSignal(str)
    tool_event = pyqtSignal(object)


# Поле ввода сообщений
class MessageInput(QPlainTextEdit):

    sendRequested = pyqtSignal()

    def keyPressEvent(self, event):

        if (
            event.key() in (Qt.Key_Return, Qt.Key_Enter)
            and not event.modifiers() & Qt.ShiftModifier
        ):
            self.sendRequested.emit()
            event.accept()
            return

        super().keyPressEvent(event)


# Главное окно AI-чата
class ChatWindow(QDialog):

    def __init__(self, assistant):
        super().__init__()

        self._assistant = assistant
        self._busy = False
        self._cancel_event = threading.Event()

        # Настройки окна
        self.setWindowTitle(
            "AI Assistant - STE Slicer"
        )

        self.resize(720, 600)
        self.setMinimumSize(440, 360)

        layout = QVBoxLayout(self)

        # Заголовок
        heading = QLabel(
            "Помощник по STE Slicer и 3D/5D-печати"
        )
        layout.addWidget(heading)

        # История сообщений
        self.messages = QTextBrowser()
        self.messages.setOpenExternalLinks(False)

        layout.addWidget(
            self.messages,
            1
        )

        # Поле для ввода
        self.input = MessageInput()

        self.input.setPlaceholderText(
            "Напиши сообщение…"
        )

        self.input.setMaximumHeight(110)

        layout.addWidget(self.input)

        # Кнопки
        buttons = QHBoxLayout()

        settings = QPushButton(
            "Настройки LLM"
        )

        settings.clicked.connect(
            assistant.showSettings
        )

        buttons.addWidget(settings)
        self.cancel_button = QPushButton("Отменить")
        self.cancel_button.setAutoDefault(False)
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_request)
        buttons.addWidget(self.cancel_button)

        self.open_folder_button = QPushButton("📂 Открыть папку")
        self.open_folder_button.setAutoDefault(False)
        self.open_folder_button.setToolTip("Открыть папку с сохранёнными G-Code и 3MF")
        self.open_folder_button.clicked.connect(self.open_output_folder)
        buttons.addWidget(self.open_folder_button)

        self._latest_export_dir = None
        buttons.addStretch()

        self.send_button = QPushButton(
            "Отправить"
        )

        self.send_button.setAutoDefault(False)
        settings.setAutoDefault(False)

        self.send_button.clicked.connect(
            self.send_message
        )

        buttons.addWidget(self.send_button)

        layout.addLayout(buttons)

        # Статус подключения
        self.status = QLabel(
            "Enter — отправить; "
            "Shift+Enter — новая строка"
        )

        self.status.setWordWrap(True)

        layout.addWidget(self.status)

        # Связь фонового потока с Qt
        self._bridge = ReplyBridge(self)

        self._bridge.received.connect(
            self.receive_reply,
            Qt.QueuedConnection
        )

        self._bridge.tool_event.connect(
            self.show_tool_event, Qt.QueuedConnection
        )

        self.input.sendRequested.connect(
            self.send_message
        )

    # Добавление сообщения в чат
    def append_message(self, author, text):

        cursor = self.messages.textCursor()

        cursor.movePosition(
            cursor.End
        )

        cursor.insertText(
            author + "\n" + text + "\n\n"
        )

        self.messages.setTextCursor(cursor)
        self.messages.ensureCursorVisible()

    # Отправка сообщения пользователя
    def send_message(self):

        text = self.input.toPlainText().strip()

        if not text or self._busy:
            return

        if len(text) > 24000:
            self.status.setText("Сократите сообщение до 24000 символов.")
            return
        self._cancel_event = threading.Event()
        cancel_event = self._cancel_event
        request_text = text
        self.cancel_button.setEnabled(True)

        # Блокируем повторную отправку
        self._busy = True

        self.send_button.setEnabled(False)

        self.append_message(
            "Вы",
            text
        )

        self.input.clear()

        self.status.setText(
            "Ожидаем ответ…"
        )

        # Получаем настройки слайсера
        # в главном потоке Qt
        try:
            settings = (
                self._assistant.get_print_settings()
            )

            settings_error = None

        except Exception as error:
            settings = None
            settings_error = str(error)

        # Фоновая функция для обращения к LLM
        def request():

            try:
                reply = self._assistant.ask_llm(
                    request_text,
                    print_settings=settings,
                    progress=self._bridge.tool_event.emit,
                    cancel_event=cancel_event
                )

                # Уведомление, если настройки
                # прочитать не удалось
                if settings_error:
                    reply += (
                        "\n\nНе удалось прочитать "
                        "настройки слайсера: "
                        + settings_error
                    )

            except Exception:
                reply = (
                    "Не удалось получить ответ. "
                    "Проверьте настройки LLM."
                )

            # Передаём ответ в главный поток Qt
            self._bridge.received.emit(reply)

        # Создание фонового потока
        worker = threading.Thread(
            target=request
        )

        worker.daemon = True
        worker.start()

    def show_tool_event(self, event):
        if event.get("phase") == "status":
            self.status.setText(event.get("message", ""))
            return
        name = event.get("name", "")
        if event.get("phase") == "start":
            self.status.setText("Выполняется инструмент: " + name)
            self.append_message("Инструмент: " + name,
                                json.dumps(event.get("arguments", {}), ensure_ascii=False))
            return
        output = event.get("output", {})
        if not output.get("ok"):
            self.append_message("Ошибка инструмента", output.get("error", "Неизвестная ошибка"))
        elif name in ("set_print_settings", "set_material"):
            changes = output.get("result", {}).get("changes", [])
            text = "\n".join("{0}: {1} → {2}".format(c["key"], c["before"], c["after"])
                             for c in changes)
            self.append_message("Изменения применены", text or "Без изменений")
        elif name == "list_print_settings":
            count = len(output.get("result", {}).get("settings", [])) if output.get("ok") else 0
            self.append_message("Поиск параметров", "Найдено настроек: {0}".format(count))
        elif name == "list_materials":
            count = len(output.get("result", {}).get("materials", [])) if output.get("ok") else 0
            self.append_message("Поиск материалов", "Найдено профилей: {0}".format(count))
        elif name == "auto_export":
            if output.get("ok"):
                res = output.get("result", {})
                self._latest_export_dir = res.get("target_dir")
                files_str = "\n".join("  • " + os.path.basename(f) for f in res.get("saved_files", []))
                self.append_message("Автоэкспорт", "Файлы сохранены в:\n{0}\n{1}".format(self._latest_export_dir, files_str))
            else:
                self.append_message("Автоэкспорт", "Ошибка: " + str(output.get("error", "неизвестная ошибка")))
        else:
            self.append_message("Результат: " + name,
                                json.dumps(output.get("result"), ensure_ascii=False, indent=2))

    # Получение ответа нейросети
    def receive_reply(self, reply):

        self.append_message(
            "Помощник",
            reply
        )

        self._busy = False
        self.cancel_button.setEnabled(False)

        self.send_button.setEnabled(True)

        self.status.setText(
            "Ответ получен."
        )

        self.input.setFocus()

    def open_output_folder(self):
        folder = self._latest_export_dir
        if not folder or not os.path.exists(folder):
            from .AutoExport import get_output_base_dir
            folder = get_output_base_dir()
        if os.path.exists(folder):
            QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
        else:
            self.status.setText("Папка экспорта пока не создана.")

    def cancel_request(self):
        self._cancel_event.set()
        self.cancel_button.setEnabled(False)
        self.status.setText("Останавливаем запрос. Уже применённые настройки сохраняются.")

    # Обработка закрытия окна
    def closeEvent(self, event):

        # Скрываем окно, сохраняя переписку
        self.hide()
        event.accept()
