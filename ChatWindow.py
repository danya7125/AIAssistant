import threading

from PyQt5.QtCore import QObject, Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPlainTextEdit,
    QPushButton, QTextBrowser, QVBoxLayout
)


class ReplyBridge(QObject):
    received = pyqtSignal(str)


class MessageInput(QPlainTextEdit):
    sendRequested = pyqtSignal()

    def keyPressEvent(self, event):
        if (event.key() in (Qt.Key_Return, Qt.Key_Enter)
                and not event.modifiers() & Qt.ShiftModifier):
            self.sendRequested.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class ChatWindow(QDialog):
    def __init__(self, assistant):
        super().__init__()
        self._assistant = assistant
        self._busy = False
        self.setWindowTitle("AI Assistant - STE Slicer")
        self.resize(720, 600)
        self.setMinimumSize(440, 360)

        layout = QVBoxLayout(self)
        heading = QLabel("Помощник по STE Slicer и 3D/5D-печати")
        layout.addWidget(heading)

        self.messages = QTextBrowser()
        self.messages.setOpenExternalLinks(False)
        layout.addWidget(self.messages, 1)

        self.input = MessageInput()
        self.input.setPlaceholderText("Напиши сообщение…")
        self.input.setMaximumHeight(110)
        layout.addWidget(self.input)

        buttons = QHBoxLayout()
        settings = QPushButton("Настройки LLM")
        settings.clicked.connect(assistant.showSettings)
        buttons.addWidget(settings)
        buttons.addStretch()
        self.send_button = QPushButton("Отправить")
        self.send_button.setAutoDefault(False)
        settings.setAutoDefault(False)
        self.send_button.clicked.connect(self.send_message)
        buttons.addWidget(self.send_button)
        layout.addLayout(buttons)

        self.status = QLabel("Enter — отправить; Shift+Enter — новая строка")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        # Qt доставляет результат в главный поток, где можно менять виджеты.
        self._bridge = ReplyBridge(self)
        self._bridge.received.connect(self.receive_reply, Qt.QueuedConnection)
        self.input.sendRequested.connect(self.send_message)

    def append_message(self, author, text):
        cursor = self.messages.textCursor()
        cursor.movePosition(cursor.End)
        cursor.insertText(author + "\n" + text + "\n\n")
        self.messages.setTextCursor(cursor)
        self.messages.ensureCursorVisible()

    def send_message(self):
        text = self.input.toPlainText().strip()
        if not text or self._busy:
            return
        self._busy = True
        self.send_button.setEnabled(False)
        self.append_message("Вы", text)
        self.input.clear()
        self.status.setText("Ожидаем ответ…")

        def request():
            try:
                reply = self._assistant.ask_llm(text)
            except Exception:
                reply = "Не удалось получить ответ. Проверьте настройки LLM."
            self._bridge.received.emit(reply)

        worker = threading.Thread(target=request)
        worker.daemon = True
        worker.start()

    def receive_reply(self, reply):
        self.append_message("Помощник", reply)
        self._busy = False
        self.send_button.setEnabled(True)
        self.status.setText("Ответ получен.")
        self.input.setFocus()

    def closeEvent(self, event):
        # При закрытии сохраняем окно и историю, включая незавершённый запрос.
        self.hide()
        event.accept()
