
import threading
import json

from PyQt5.QtCore import QObject, Qt, pyqtSignal

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
                    text,
                    print_settings=settings,
                    progress=self._bridge.tool_event.emit
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
        name = event.get("name", "")
        if event.get("phase") == "start":
            self.status.setText("Выполняется инструмент: " + name)
            self.append_message("Инструмент: " + name,
                                json.dumps(event.get("arguments", {}), ensure_ascii=False))
            return
        output = event.get("output", {})
        if not output.get("ok"):
            self.append_message("Ошибка инструмента", output.get("error", "Неизвестная ошибка"))
        elif name == "set_print_settings":
            changes = output.get("result", {}).get("changes", [])
            text = "\n".join("{0}: {1} → {2}".format(c["key"], c["before"], c["after"])
                             for c in changes)
            self.append_message("Изменения применены", text)
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

        self.send_button.setEnabled(True)

        self.status.setText(
            "Ответ получен."
        )

        self.input.setFocus()

    # Обработка закрытия окна
    def closeEvent(self, event):

        # Скрываем окно, сохраняя переписку
        self.hide()
        event.accept()
