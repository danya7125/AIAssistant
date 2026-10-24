'use strict';

const chatForm = document.getElementById('chat');
const messageInput = document.getElementById('text');
const sendButton = document.getElementById('send');
const messageList = document.getElementById('messages');
const profileCardTemplate = document.getElementById('profile-card-template');
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
const ALLOWED_EXTENSIONS = ['stl'];
let selectedFiles = [];
let isSending = false;
let eventCursor = 0;
let eventPolling = false;
let pendingProfileChanges = false;
let profileCardSequence = 0;
let lastAssistantMessageElement = null;

try {
    localStorage.removeItem('ai-assistant-web-attachments');
} catch (error) {
    statusText.textContent = 'Не удалось очистить старые метаданные вложений.';
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

function appendInlineFormatting(container, text) {
    const tokenPattern = /(\*\*.+?\*\*|`[^`]+`|\*[^*]+\*)/g;
    let lastIndex = 0;
    let match;

    while ((match = tokenPattern.exec(text)) !== null) {
        if (match.index > lastIndex) {
            container.appendChild(document.createTextNode(text.slice(lastIndex, match.index)));
        }
        const token = match[0];
        const element = document.createElement(
            token.slice(0, 2) === '**' ? 'strong' :
                token.charAt(0) === '`' ? 'code' : 'em'
        );
        element.textContent = token.slice(token.charAt(0) === '`' ? 1 : token.slice(0, 2) === '**' ? 2 : 1,
            token.charAt(0) === '`' ? -1 : token.slice(0, 2) === '**' ? -2 : -1);
        container.appendChild(element);
        lastIndex = tokenPattern.lastIndex;
    }

    if (lastIndex < text.length) {
        container.appendChild(document.createTextNode(text.slice(lastIndex)));
    }
}

function formatAssistantMessage(container, text) {
    let list = null;
    let listType = '';
    let paragraph = [];
    const lines = text.split(/\r?\n/);

    function flushParagraph() {
        if (!paragraph.length) {
            return;
        }
        const element = document.createElement('p');
        paragraph.forEach(function (line, index) {
            if (index) {
                element.appendChild(document.createElement('br'));
            }
            appendInlineFormatting(element, line);
        });
        container.appendChild(element);
        paragraph = [];
    }

    function closeList() {
        if (list) {
            container.appendChild(list);
            list = null;
            listType = '';
        }
    }

    function isTableSeparator(line) {
        return /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(line);
    }

    function parseTableRow(line) {
        return line.trim().replace(/^\|/, '').replace(/\|$/, '')
            .split('|').map(function (cell) { return cell.trim(); });
    }

    function appendTableCell(row, tag, value) {
        const cell = document.createElement(tag);
        appendInlineFormatting(cell, value);
        row.appendChild(cell);
    }

    function appendTable(startIndex) {
        flushParagraph();
        closeList();
        const headerCells = parseTableRow(lines[startIndex]);
        const table = document.createElement('table');
        table.className = 'assistant-table';
        const thead = document.createElement('thead');
        const headerRow = document.createElement('tr');
        headerCells.forEach(function (cell) {
            appendTableCell(headerRow, 'th', cell);
        });
        thead.appendChild(headerRow);
        table.appendChild(thead);

        const tbody = document.createElement('tbody');
        let rowIndex = startIndex + 2;
        while (rowIndex < lines.length && lines[rowIndex].trim().indexOf('|') !== -1) {
            const cells = parseTableRow(lines[rowIndex]);
            if (cells.some(function (cell) { return cell !== ''; })) {
                const row = document.createElement('tr');
                headerCells.forEach(function (_, index) {
                    appendTableCell(row, 'td', cells[index] || '');
                });
                tbody.appendChild(row);
            }
            rowIndex += 1;
        }
        table.appendChild(tbody);
        const tableWrapper = document.createElement('div');
        tableWrapper.className = 'assistant-table-wrap';
        tableWrapper.appendChild(table);
        container.appendChild(tableWrapper);
        return rowIndex - 1;
    }

    for (let lineIndex = 0; lineIndex < lines.length; lineIndex += 1) {
        const line = lines[lineIndex];
        const heading = /^(#{1,3})\s+(.+)$/.exec(line);
        const bullet = /^\s*[-*]\s+(.+)$/.exec(line);
        const numbered = /^\s*\d+[.)]\s+(.+)$/.exec(line);

        if (line.indexOf('|') !== -1 && lineIndex + 1 < lines.length && isTableSeparator(lines[lineIndex + 1])) {
            lineIndex = appendTable(lineIndex);
            continue;
        }
        if (!line.trim()) {
            flushParagraph();
            closeList();
            continue;
        }
        if (heading) {
            flushParagraph();
            closeList();
            const headingElement = document.createElement('h3');
            appendInlineFormatting(headingElement, heading[2]);
            container.appendChild(headingElement);
            continue;
        }
        if (bullet || numbered) {
            flushParagraph();
            const nextListType = bullet ? 'ul' : 'ol';
            if (list && listType !== nextListType) {
                closeList();
            }
            if (!list) {
                list = document.createElement(nextListType);
                listType = nextListType;
            }
            const item = document.createElement('li');
            appendInlineFormatting(item, (bullet || numbered)[1]);
            list.appendChild(item);
            continue;
        }

        closeList();
        paragraph.push(line);
    }

    flushParagraph();
    closeList();
}

function shouldShowProfileCard(text, requestText, hasProfileChanges) {
    return hasProfileChanges
        || /(?:профил|настройк\w*\s+(?:печати|слайсера)|параметр\w*\s+(?:печати|слайсера)|контекст проекта|текущ\w*\s+(?:настройк|параметр|профил)|заполнени|скорост\w*\s+печати|температур\w*\s+(?:сопла|стола)|высот\w*\s+слоя|материал|сопло|периметр)/i.test(text)
        || /(?:профил|настройк\w*\s+(?:печати|слайсера)|параметр\w*\s+(?:печати|слайсера)|покажи.*(?:информац|параметр|настройк|профил|значен)|выведи.*информац|файл|модель)/i.test(requestText);
}

function appendProfileCard(messageElement) {
    if (!profileCardTemplate || !profileCardTemplate.content.firstElementChild) {
        return;
    }
    const card = profileCardTemplate.content.firstElementChild.cloneNode(true);
    profileCardSequence += 1;
    const ids = new Map();
    card.querySelectorAll('[id]').forEach(function (element) {
        const originalId = element.id;
        const uniqueId = originalId + '-' + profileCardSequence;
        ids.set(originalId, uniqueId);
        element.id = uniqueId;
    });
    card.querySelectorAll('[aria-labelledby], [aria-describedby], [aria-controls]').forEach(function (element) {
        ['aria-labelledby', 'aria-describedby', 'aria-controls'].forEach(function (attribute) {
            const value = element.getAttribute(attribute);
            if (value) {
                element.setAttribute(attribute, value.split(/\s+/).map(function (id) {
                    return ids.get(id) || id;
                }).join(' '));
            }
        });
    });
    const labelledBy = card.getAttribute('aria-labelledby');
    if (labelledBy) {
        card.setAttribute('aria-labelledby', labelledBy.split(/\s+/).map(function (id) {
            return ids.get(id) || id;
        }).join(' '));
    }

    const profileContainer = document.createElement('div');
    profileContainer.className = 'message-profile';
    profileContainer.appendChild(card);
    messageElement.appendChild(profileContainer);
}

function addMessage(author, text, type, attachments, requestText, hasProfileChanges) {
    const messageElement = document.createElement('article');
    messageElement.className = 'message ' + type;

    const authorElement = document.createElement('span');
    authorElement.className = 'message-author';
    authorElement.textContent = author;

    const bodyElement = document.createElement('div');
    bodyElement.className = 'message-body';
    if (type === 'assistant') {
        formatAssistantMessage(bodyElement, text);
    } else {
        bodyElement.textContent = text;
    }

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
        disclaimer.textContent = 'Вложение будет передано локальному помощнику для этого запроса.';

        messageElement.appendChild(attachmentContainer);
        messageElement.appendChild(disclaimer);
    }

    if (type === 'assistant' && shouldShowProfileCard(text, requestText || '', hasProfileChanges)) {
        appendProfileCard(messageElement);
    }

    messageList.appendChild(messageElement);
    if (type === 'assistant') {
        lastAssistantMessageElement = messageElement;
    }
    messageList.scrollTop = messageList.scrollHeight;
    return messageElement;
}

function addActivity(eventData) {
    if (!eventData || typeof eventData !== 'object') {
        return;
    }

    const toolName = eventData.tool || eventData.name || eventData.type || 'Инструмент';
    const toolLabels = {
        get_print_context: 'Контекст слайсера',
        get_print_settings: 'Текущие параметры',
        get_scene_models: 'Модели на столе',
        set_material: 'Материал',
        set_print_settings: 'Изменение параметров',
        load_model: 'Загрузка модели',
        orient_model: 'Ориентация модели',
        arrange_models: 'Расстановка моделей',
        validate_geometry: 'Проверка геометрии',
        start_slicing: 'Слайсинг'
    };
    const displayName = toolLabels[toolName] || toolName;
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
    label.textContent = String(displayName) + status;
    title.appendChild(bullet);
    title.appendChild(label);
    item.appendChild(title);

    if (result !== '' && result !== null) {
        if (result && typeof result === 'object' && Array.isArray(result.changes)) {
            const changes = document.createElement('div');
            changes.className = 'activity-changes';
            const settingLabels = {
                material: 'Материал',
                machine_nozzle_size: 'Сопло',
                material_print_temperature: 'Температура сопла',
                material_bed_temperature: 'Температура стола',
                layer_height: 'Высота слоя',
                speed_print: 'Скорость печати',
                speed_wall: 'Скорость внешней стенки',
                speed_layer_0: 'Скорость первого слоя',
                infill_sparse_density: 'Заполнение',
                wall_line_count: 'Периметры',
                loaded_model: 'Загружена модель'
            };
            result.changes.forEach(function (change) {
                const row = document.createElement('p');
                row.className = 'activity-change';
                const key = document.createElement('span');
                key.className = 'activity-change-key';
                key.textContent = settingLabels[change.key] || change.key;
                const value = document.createElement('span');
                value.className = 'activity-change-value';
                value.textContent = formatProfileValue(change.before, '') + ' → ' +
                    formatProfileValue(change.after, '');
                row.appendChild(key);
                row.appendChild(value);
                changes.appendChild(row);
            });
            item.appendChild(changes);
        } else {
            const resultElement = document.createElement('p');
            resultElement.className = 'activity-result';
            resultElement.textContent = typeof result === 'string'
                ? result
                : JSON.stringify(result);
            item.appendChild(resultElement);
        }
    }

    activityList.appendChild(item);
    activityEmpty.hidden = true;
}

function formatProfileValue(value, unit) {
    if (value === null || value === undefined || value === '') {
        return '—';
    }
    if (typeof value === 'number' && Number.isFinite(value)) {
        value = Number.isInteger(value) ? String(value) : String(Number(value.toFixed(2)));
    } else {
        value = String(value);
    }
    return value + unit;
}

function refreshProfile(changedKeys) {
    return fetch('/api/profile')
        .then(function (response) {
            if (!response.ok) {
                throw new Error('HTTP ' + response.status);
            }
            return response.json();
        })
        .then(function (profile) {
            const context = profile.context || {};
            const settings = profile.settings || {};
            const mode = settings.printing_mode === 'classic' ? 'FDM' : settings.printing_mode;
            const model = context.models && context.models.length ? context.models[0] : null;

            document.querySelectorAll('.slice-profile').forEach(function (profileCard) {
                const title = profileCard.querySelector('[data-profile-title]');
                if (title) {
                    title.textContent = (mode ? mode + ' · ' : '') +
                        (context.profile || context.printer && context.printer.name || 'Активные настройки');
                }

                const material = profileCard.querySelector('[data-profile-material]');
                if (material) {
                    material.textContent = context.material || '—';
                    if (changedKeys && changedKeys.indexOf('material') !== -1) {
                        material.classList.remove('is-updated');
                        void material.offsetWidth;
                        material.classList.add('is-updated');
                        setTimeout(function () {
                            material.classList.remove('is-updated');
                        }, 1800);
                    }
                }

                const modelName = profileCard.querySelector('[data-model-name]');
                const modelSize = profileCard.querySelector('[data-model-size]');
                if (modelName) {
                    modelName.textContent = model ? model.name : 'Модель не загружена';
                }
                if (modelSize) {
                    const size = model && model.size_mm;
                    modelSize.textContent = size
                        ? 'Размеры: ' + ['x', 'y', 'z'].map(function (axis) {
                            return formatProfileValue(size[axis], ' мм');
                        }).join(' × ')
                        : 'Размеры появятся после загрузки модели';
                }

                profileCard.querySelectorAll('[data-setting]').forEach(function (element) {
                    const key = element.getAttribute('data-setting');
                    if (Object.prototype.hasOwnProperty.call(settings, key)) {
                        element.textContent = formatProfileValue(
                            settings[key],
                            element.getAttribute('data-unit') || ''
                        );
                    } else {
                        element.textContent = '—';
                    }
                    if (changedKeys && changedKeys.indexOf(key) !== -1) {
                        element.classList.remove('is-updated');
                        void element.offsetWidth;
                        element.classList.add('is-updated');
                        setTimeout(function () {
                            element.classList.remove('is-updated');
                        }, 1800);
                    }
                });
            });
        })
        .catch(function (error) {
            statusText.textContent = 'Не удалось обновить профиль слайсера: ' + error.message;
        });
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
        if (output.ok && [
            'set_material',
            'set_print_settings',
            'load_model',
            'orient_model',
            'arrange_models'
        ].indexOf(eventData.name) !== -1) {
            pendingProfileChanges = true;
            const changes = output.result && output.result.changes;
            const changedKeys = Array.isArray(changes)
                ? changes.map(function (change) { return change.key; })
                : [];
            refreshProfile(changedKeys).then(function () {
                if (lastAssistantMessageElement && !lastAssistantMessageElement.querySelector('.slice-profile')) {
                    appendProfileCard(lastAssistantMessageElement);
                    refreshProfile(changedKeys);
                }
            });
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
            let lastUserText = '';
            data.messages.forEach(function (message) {
                if (!message || typeof message.content !== 'string') {
                    return;
                }
                const author = message.role === 'user' ? 'Вы' : 'Помощник';
                const type = message.role === 'user' ? 'user' : 'assistant';
                if (type === 'user') {
                    lastUserText = message.content;
                    lastAssistantMessageElement = null;
                    addMessage(author, message.content, type);
                } else {
                    addMessage(
                        author,
                        message.content,
                        type,
                        null,
                        lastUserText,
                        shouldShowProfileCard(message.content, lastUserText, false)
                    );
                }
            });
        }
        if (Number.isFinite(Number(data.event_cursor))) {
            eventCursor = Number(data.event_cursor);
        }
        statusText.textContent = 'Подключено к STE Slicer.';
        refreshProfile();
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
    pendingProfileChanges = false;
    lastAssistantMessageElement = null;
    sendButton.disabled = true;
    const userMessage = addMessage('Вы', messageText, 'user', messageAttachments);
    messageInput.value = '';
    selectedFiles = [];
    attachmentInput.value = '';
    renderAttachments();
    statusText.textContent = 'Ожидаем ответ…';

    const controller = new AbortController();
    const timeoutId = setTimeout(function () {
        controller.abort();
    }, REQUEST_TIMEOUT_MS);

    try {
        eventPolling = true;
        const requestBody = new FormData();
        requestBody.append('text', messageText);
        messageAttachments.forEach(function (file) {
            requestBody.append('attachments', file, file.name);
        });
        const response = await fetch('/api/message', {
            method: 'POST',
            body: requestBody,
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

        addMessage(
            'Помощник',
            data.reply,
            'assistant',
            null,
            messageText,
            pendingProfileChanges || messageAttachments.length > 0
        );
        const attachmentDisclaimer = userMessage.querySelector('.attachment-disclaimer');
        if (attachmentDisclaimer) {
            attachmentDisclaimer.textContent = 'Отправлено локальному помощнику; исходник не хранится.';
        }
        refreshProfile();
        statusText.textContent = 'Ответ получен.';

    } catch (error) {
        const errorText = error.name === 'AbortError'
            ? 'Сервер не ответил за ' + REQUEST_TIMEOUT_MS / 1000 + ' секунд.'
            : error.message;

        addMessage('Ошибка', errorText, 'error');
        const attachmentDisclaimer = userMessage.querySelector('.attachment-disclaimer');
        if (attachmentDisclaimer) {
            attachmentDisclaimer.textContent = 'Не удалось отправить. Прикрепите файл повторно.';
        }
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
        statusText.textContent = 'Принимаются только файлы STL. Не поддерживаются: ' +
            rejectedFiles.join(', ');
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

refreshProfile();
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
