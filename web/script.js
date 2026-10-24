'use strict';

const chatForm = document.getElementById('chat');
const messageInput = document.getElementById('text');
const sendButton = document.getElementById('send');
const messageList = document.getElementById('messages');
const statusText = document.getElementById('status');
const attachmentInput = document.getElementById('attachments');
const attachmentList = document.getElementById('attachment-list');
const activityList = document.getElementById('activity-list');
const activityEmpty = document.getElementById('activity-empty');
const slicingState = document.getElementById('slicing-state');
const slicingStatusText = document.getElementById('slicing-status');
const slicingProgress = document.getElementById('slicing-progress');
const printTime = document.getElementById('print-time');
const materialUsage = document.getElementById('material-usage');
const detailsToggle = document.getElementById('details-toggle');
const workspace = document.querySelector('.workspace');

const REQUEST_TIMEOUT_MS = 200000;
const MAX_ATTACHMENTS = 8;
const ALLOWED_EXTENSIONS = ['jpg', 'jpeg', 'png', 'stl', 'obj', '3mf'];
const ATTACHMENT_HISTORY_KEY = 'ai-assistant-web-attachments';
let selectedFiles = [];
let isSending = false;
let eventCursor = 0;
let eventPolling = false;
let storedAttachmentMessages = loadStoredAttachmentMessages();

function loadStoredAttachmentMessages() {
    try {
        const stored = JSON.parse(localStorage.getItem(ATTACHMENT_HISTORY_KEY) || '[]');
        return Array.isArray(stored) ? stored : [];
    } catch (error) {
        statusText.textContent = 'Не удалось восстановить вложения из истории браузера.';
        return [];
    }
}

function saveStoredAttachmentMessage(text, files) {
    if (!files.length) {
        return;
    }

    storedAttachmentMessages.push({
        text: text,
        files: files.map(function (file) {
            return {
                name: file.name,
                size: file.size,
                type: file.type || ''
            };
        })
    });
    storedAttachmentMessages = storedAttachmentMessages.slice(-20);

    try {
        localStorage.setItem(
            ATTACHMENT_HISTORY_KEY,
            JSON.stringify(storedAttachmentMessages)
        );
    } catch (error) {
        statusText.textContent = 'Вложения останутся в текущем чате, но браузер не сохранил их историю.';
    }
}

function findStoredAttachments(text, usedIndexes) {
    for (let index = 0; index < storedAttachmentMessages.length; index += 1) {
        if (!usedIndexes.has(index) && storedAttachmentMessages[index].text === text) {
            usedIndexes.add(index);
            return storedAttachmentMessages[index].files;
        }
    }
    return [];
}

detailsToggle.addEventListener('click', function () {
    const expanded = detailsToggle.getAttribute('aria-expanded') === 'true';
    detailsToggle.setAttribute('aria-expanded', String(!expanded));
    workspace.classList.toggle('details-open', !expanded);
});

function getExtension(fileName) {
    const parts = fileName.toLowerCase().split('.');
    return parts.length > 1 ? parts[parts.length - 1] : '';
}

function formatFileSize(size) {
    if (size < 1024) {
        return size + ' Б';
    }
    if (size < 1024 * 1024) {
        return Math.round(size / 1024) + ' КБ';
    }
    return (size / (1024 * 1024)).toFixed(1) + ' МБ';
}

function renderAttachments() {
    while (attachmentList.firstChild) {
        attachmentList.removeChild(attachmentList.firstChild);
    }

    selectedFiles.forEach(function (file, index) {
        const chip = document.createElement('span');
        chip.className = 'attachment-chip';

        const name = document.createElement('span');
        name.className = 'attachment-name';
        name.textContent = file.name;

        const size = document.createElement('span');
        size.className = 'attachment-size';
        size.textContent = formatFileSize(file.size);

        const removeButton = document.createElement('button');
        removeButton.className = 'remove-attachment';
        removeButton.type = 'button';
        removeButton.textContent = '×';
        removeButton.setAttribute('aria-label', 'Удалить файл ' + file.name);
        removeButton.addEventListener('click', function () {
            selectedFiles.splice(index, 1);
            renderAttachments();
        });

        chip.appendChild(name);
        chip.appendChild(size);
        chip.appendChild(removeButton);
        attachmentList.appendChild(chip);
    });
}

function addMessage(author, text, type, attachments) {
    const welcome = document.getElementById('welcome');
    if (welcome) {
        welcome.remove();
    }

    const messageElement = document.createElement('article');
    messageElement.className = 'message ' + type;

    const authorElement = document.createElement('span');
    authorElement.className = 'message-author';
    authorElement.textContent = author;

    const bodyElement = document.createElement('p');
    bodyElement.className = 'message-body';
    bodyElement.textContent = text;

    messageElement.appendChild(authorElement);
    messageElement.appendChild(bodyElement);

    if (attachments && attachments.length) {
        const attachmentContainer = document.createElement('div');
        attachmentContainer.className = 'message-attachments';

        attachments.forEach(function (file) {
            const attachment = document.createElement('div');
            attachment.className = 'message-attachment';

            const icon = document.createElement('span');
            icon.className = 'message-attachment-icon';
            icon.setAttribute('aria-hidden', 'true');
            icon.textContent = getExtension(file.name).toUpperCase();

            const details = document.createElement('span');
            details.className = 'message-attachment-details';

            const name = document.createElement('strong');
            name.className = 'message-attachment-name';
            name.textContent = file.name;

            const size = document.createElement('small');
            size.className = 'message-attachment-size';
            size.textContent = formatFileSize(file.size);

            details.appendChild(name);
            details.appendChild(size);
            attachment.appendChild(icon);
            attachment.appendChild(details);
            attachmentContainer.appendChild(attachment);
        });

        const disclaimer = document.createElement('span');
        disclaimer.className = 'attachment-disclaimer';
        disclaimer.textContent = 'Файлы останутся в переписке. Их содержимое пока не передаётся помощнику.';

        messageElement.appendChild(attachmentContainer);
        messageElement.appendChild(disclaimer);
    }

    messageList.appendChild(messageElement);
    messageList.scrollTop = messageList.scrollHeight;
}

function addActivity(eventData) {
    if (!eventData || typeof eventData !== 'object') {
        return;
    }

    const toolName = eventData.tool || eventData.name || eventData.type || 'Инструмент';
    const status = eventData.status ? ' · ' + eventData.status : '';
    const result = eventData.result || eventData.output || eventData.message || '';

    const item = document.createElement('li');
    item.className = 'activity-item';

    const title = document.createElement('div');
    title.className = 'activity-title';

    const bullet = document.createElement('span');
    bullet.className = 'activity-bullet';
    bullet.setAttribute('aria-hidden', 'true');

    const label = document.createElement('span');
    label.textContent = String(toolName) + status;
    title.appendChild(bullet);
    title.appendChild(label);
    item.appendChild(title);

    if (result !== '' && result !== null) {
        const resultElement = document.createElement('p');
        resultElement.className = 'activity-result';
        resultElement.textContent = typeof result === 'string'
            ? result
            : JSON.stringify(result);
        item.appendChild(resultElement);
    }

    activityList.appendChild(item);
    activityEmpty.hidden = true;
}

function updateSlicing(data) {
    if (!data || typeof data !== 'object') {
        return;
    }

    const status = typeof data.status === 'string' ? data.status : '';
    slicingStatusText.textContent = status || 'Слайсинг обновлён';
    slicingState.classList.remove('is-active', 'is-complete', 'is-error');

    const normalizedStatus = status.toLowerCase();
    if (/error|ошиб/.test(normalizedStatus)) {
        slicingState.classList.add('is-error');
    } else if (/done|complete|готов|заверш/.test(normalizedStatus)) {
        slicingState.classList.add('is-complete');
    } else if (/start|progress|slice|выполн|подготов|слайс/.test(normalizedStatus)) {
        slicingState.classList.add('is-active');
    }

    const progress = data.progress_percent !== undefined
        ? Number(data.progress_percent)
        : Number(data.progress) * (Number(data.progress) <= 1 ? 100 : 1);
    if (Number.isFinite(progress)) {
        slicingProgress.value = Math.max(0, Math.min(100, progress));
    }

    if (data.print_time !== undefined && data.print_time !== null) {
        printTime.textContent = String(data.print_time);
    }
    if (data.material !== undefined && data.material !== null) {
        materialUsage.textContent = String(data.material);
    }
}

function displayAgentEvents(events) {
    if (!Array.isArray(events)) {
        return;
    }

    events.forEach(function (entry) {
        if (entry && Number.isFinite(Number(entry.id))) {
            eventCursor = Math.max(eventCursor, Number(entry.id));
            handleAgentEvent(entry.event);
        }
    });
}

function handleAgentEvent(eventData) {
    if (!eventData || typeof eventData !== 'object') {
        return;
    }

    if (eventData.phase === 'status') {
        const message = eventData.message || 'Обработка запроса…';
        statusText.textContent = message;
        const slicingProgressMatch = /^Нарезка:\s*([\d.]+)%/.exec(message);
        if (slicingProgressMatch) {
            updateSlicing({
                status: 'processing',
                progress_percent: Number(slicingProgressMatch[1])
            });
        }
        return;
    }

    if (eventData.phase === 'start') {
        addActivity({
            name: eventData.name || 'Инструмент',
            status: 'выполняется',
            result: eventData.arguments || ''
        });
        return;
    }

    if (eventData.phase === 'result') {
        const output = eventData.output || {};
        const result = output.ok ? output.result : output.error;
        addActivity({
            name: eventData.name || 'Инструмент',
            status: output.ok ? 'готово' : 'ошибка',
            result: result || 'Нет данных о результате.'
        });

        if (eventData.name === 'start_slicing' && output.result) {
            updateSlicing(output.result);
        }
    }
}

async function pollAgentEvents() {
    if (!eventPolling) {
        return;
    }

    try {
        const response = await fetch('/api/events?since=' + eventCursor);
        if (!response.ok) {
            throw new Error('HTTP ' + response.status);
        }
        const data = await response.json();
        displayAgentEvents(data.events);
        if (Number.isFinite(Number(data.next_id))) {
            eventCursor = Math.max(eventCursor, Number(data.next_id));
        }
    } catch (error) {
        statusText.textContent = 'Нет соединения с сервером STE Slicer.';
    }
}

async function loadConversation() {
    try {
        const response = await fetch('/api/history');
        if (!response.ok) {
            throw new Error('HTTP ' + response.status);
        }
        const data = await response.json();
        if (Array.isArray(data.messages)) {
            const usedAttachmentIndexes = new Set();
            data.messages.forEach(function (message) {
                if (!message || typeof message.content !== 'string') {
                    return;
                }
                const author = message.role === 'user' ? 'Вы' : 'Помощник';
                const type = message.role === 'user' ? 'user' : 'assistant';
                const attachments = message.role === 'user'
                    ? findStoredAttachments(message.content, usedAttachmentIndexes)
                    : [];
                addMessage(author, message.content, type, attachments);
            });
        }
        if (Number.isFinite(Number(data.event_cursor))) {
            eventCursor = Number(data.event_cursor);
        }
        statusText.textContent = 'Подключено к STE Slicer.';
    } catch (error) {
        statusText.textContent = 'Не удалось подключиться к серверу STE Slicer.';
    }

    eventPolling = true;
    pollAgentEvents();
    setInterval(pollAgentEvents, 500);
}

async function sendMessage(event) {
    event.preventDefault();

    const typedText = messageInput.value.trim();
    if ((!typedText && !selectedFiles.length) || isSending) {
        return;
    }

    const messageAttachments = selectedFiles.slice();
    const messageText = typedText || 'Прикреплённые файлы: ' +
        messageAttachments.map(function (file) {
            return file.name;
        }).join(', ');
    isSending = true;
    sendButton.disabled = true;
    addMessage('Вы', messageText, 'user', messageAttachments);
    saveStoredAttachmentMessage(messageText, messageAttachments);
    messageInput.value = '';
    statusText.textContent = 'Ожидаем ответ…';

    const controller = new AbortController();
    const timeoutId = setTimeout(function () {
        controller.abort();
    }, REQUEST_TIMEOUT_MS);

    try {
        eventPolling = true;
        const response = await fetch('/api/message', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ text: messageText }),
            signal: controller.signal
        });

        if (!response.ok) {
            const errorData = await response.json();
            throw new Error(errorData.error || 'HTTP ' + response.status);
        }

        const data = await response.json();
        if (typeof data.reply !== 'string') {
            throw new Error('В ответе нет поля reply');
        }

        addMessage('Помощник', data.reply, 'assistant');
        statusText.textContent = 'Ответ получен.';

        selectedFiles = [];
        attachmentInput.value = '';
        renderAttachments();
    } catch (error) {
        const errorText = error.name === 'AbortError'
            ? 'Сервер не ответил за ' + REQUEST_TIMEOUT_MS / 1000 + ' секунд.'
            : error.message;

        addMessage('Ошибка', errorText, 'error');
        statusText.textContent =
            'Проверьте, что STE Slicer запущен и чат открыт через меню плагина.';

    } finally {
        clearTimeout(timeoutId);
        isSending = false;
        sendButton.disabled = false;
        messageInput.focus();
    }
}

attachmentInput.addEventListener('change', function () {
    const rejectedFiles = [];
    const acceptedFiles = [];

    Array.prototype.forEach.call(attachmentInput.files, function (file) {
        if (ALLOWED_EXTENSIONS.indexOf(getExtension(file.name)) === -1) {
            rejectedFiles.push(file.name);
        } else {
            acceptedFiles.push(file);
        }
    });

    const existingFiles = new Set(selectedFiles.map(function (file) {
        return file.name + ':' + file.size + ':' + file.lastModified;
    }));
    const uniqueFiles = acceptedFiles.filter(function (file) {
        const key = file.name + ':' + file.size + ':' + file.lastModified;
        if (existingFiles.has(key)) {
            return false;
        }
        existingFiles.add(key);
        return true;
    });
    const availableSlots = MAX_ATTACHMENTS - selectedFiles.length;
    selectedFiles = selectedFiles.concat(uniqueFiles.slice(0, availableSlots));
    renderAttachments();

    if (rejectedFiles.length) {
        statusText.textContent = 'Не поддерживаются: ' + rejectedFiles.join(', ');
    } else if (uniqueFiles.length > availableSlots) {
        statusText.textContent = 'Можно прикрепить не более ' + MAX_ATTACHMENTS + ' файлов.';
    } else if (uniqueFiles.length) {
        statusText.textContent = 'Файлы прикреплены к сообщению.';
    }

    attachmentInput.value = '';
});

chatForm.addEventListener('dragover', function (event) {
    event.preventDefault();
    chatForm.classList.add('is-dragging');
});

chatForm.addEventListener('dragleave', function (event) {
    if (!chatForm.contains(event.relatedTarget)) {
        chatForm.classList.remove('is-dragging');
    }
});

chatForm.addEventListener('drop', function (event) {
    event.preventDefault();
    chatForm.classList.remove('is-dragging');

    const droppedFiles = Array.prototype.slice.call(event.dataTransfer.files);
    const transfer = new DataTransfer();
    droppedFiles.forEach(function (file) {
        transfer.items.add(file);
    });

    const previousFiles = selectedFiles.slice();
    attachmentInput.files = transfer.files;
    attachmentInput.dispatchEvent(new Event('change'));
    const newlyAdded = selectedFiles.filter(function (file) {
        return previousFiles.indexOf(file) === -1;
    });
    if (newlyAdded.length) {
        statusText.textContent = 'Файлы прикреплены к сообщению.';
    }
});

chatForm.addEventListener('submit', sendMessage);

messageInput.addEventListener('keydown', function (event) {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        chatForm.requestSubmit();
    }
});

loadConversation();
messageInput.focus();

const openFolderBtn = document.getElementById('open-folder-btn');
if (openFolderBtn) {
    openFolderBtn.addEventListener('click', async function () {
        try {
            const resp = await fetch('/api/open-folder');
            if (!resp.ok) {
                alert('Папка с экспортом пока не создана.');
            }
        } catch (e) {
            console.error(e);
        }
    });
}
