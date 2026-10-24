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
const clearChatButton = document.getElementById('clear-chat');
const CLEAR_MARKER_KEY = 'ai-assistant-web-cleared-tail';

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

/* ============================================================
   Карточка профиля: данные от ИИ + 3D-превью загруженной модели
   ============================================================ */

const uploadedModels = new Map();
let lastUploadedModels = [];
const activeViewers = [];
const MAX_VIEWERS = 6;
const MAX_PREVIEW_TRIANGLES = 400000;

const PARAM_ALIASES = {
    nozzle: 'machine_nozzle_size',
    nozzle_size: 'machine_nozzle_size',
    nozzle_temp: 'material_print_temperature',
    nozzle_temperature: 'material_print_temperature',
    print_temperature: 'material_print_temperature',
    bed_temp: 'material_bed_temperature',
    bed_temperature: 'material_bed_temperature',
    speed: 'speed_print',
    print_speed: 'speed_print',
    wall_speed: 'speed_wall',
    outer_wall_speed: 'speed_wall',
    first_layer_speed: 'speed_layer_0',
    infill: 'infill_sparse_density',
    infill_density: 'infill_sparse_density',
    walls: 'wall_line_count',
    perimeters: 'wall_line_count'
};

const PARAM_META = {
    material: { label: 'Материал', group: 'Материал' },
    machine_nozzle_size: { label: 'Сопло', unit: ' мм', group: 'Печать' },
    material_print_temperature: { label: 'Температура сопла', unit: '°C', group: 'Печать' },
    material_bed_temperature: { label: 'Температура стола', unit: '°C', group: 'Печать' },
    layer_height: { label: 'Высота слоя', unit: ' мм', group: 'Печать' },
    line_width: { label: 'Ширина линии', unit: ' мм', group: 'Печать' },
    wall_line_count: { label: 'Периметры', group: 'Печать' },
    top_layers: { label: 'Верхние слои', group: 'Печать' },
    bottom_layers: { label: 'Нижние слои', group: 'Печать' },
    printing_mode: { label: 'Режим печати', group: 'Печать' },
    speed_print: { label: 'Печать', unit: ' мм/с', group: 'Скорости' },
    speed_wall: { label: 'Внешняя стенка', unit: ' мм/с', group: 'Скорости' },
    speed_layer_0: { label: 'Первый слой', unit: ' мм/с', group: 'Скорости' },
    infill_sparse_density: { label: 'Заполнение', unit: '%', group: 'Заполнение' },
    infill_pattern: { label: 'Шаблон заполнения', group: 'Заполнение' },
    cool_fan_speed: { label: 'Обдув', unit: '%', group: 'Прочее' },
    retraction_amount: { label: 'Откат', unit: ' мм', group: 'Прочее' },
    adhesion_type: { label: 'Адгезия', group: 'Прочее' },
    brim_width: { label: 'Кайма', unit: ' мм', group: 'Прочее' },
    support_enable: { label: 'Поддержки', group: 'Прочее' }
};

const GROUP_ORDER = ['Материал', 'Печать', 'Скорости', 'Заполнение', 'Прочее'];

function tryParseJson(source) {
    try {
        const value = JSON.parse(String(source).trim());
        return value && typeof value === 'object' ? value : null;
    } catch (error) {
        return null;
    }
}

function normalizeCardObject(value) {
    if (!value || typeof value !== 'object') {
        return null;
    }
    const card = value.card && typeof value.card === 'object' ? value.card : value;
    const looksLikeCard = card.action === 'show_profile_card' || value.action === 'show_profile_card' ||
        (card.params && typeof card.params === 'object') || Array.isArray(card.rows);
    return looksLikeCard ? card : null;
}

/* Ищет в ответе ИИ команду карточки: [CARD]{...}[/CARD], блок ```json или чистый JSON. */
function extractCard(text) {
    const result = { card: null, rest: text };

    const tagged = /\[CARD\]([\s\S]*?)\[\/CARD\]/i.exec(text);
    if (tagged) {
        const card = normalizeCardObject(tryParseJson(tagged[1]));
        if (card) {
            result.card = card;
            result.rest = text.replace(tagged[0], '').trim();
            return result;
        }
    }

    const fence = /```(?:json)?\s*([\s\S]*?)```/gi;
    let match;
    while ((match = fence.exec(text)) !== null) {
        const card = normalizeCardObject(tryParseJson(match[1]));
        if (card) {
            result.card = card;
            result.rest = text.replace(match[0], '').trim();
            return result;
        }
    }

    const trimmed = text.trim();
    if (trimmed.charAt(0) === '{' && trimmed.charAt(trimmed.length - 1) === '}') {
        const card = normalizeCardObject(tryParseJson(trimmed));
        if (card) {
            result.card = card;
            result.rest = '';
        }
    }
    return result;
}

function shouldShowProfileCard(text, requestText, hasProfileChanges) {
    return Boolean(hasProfileChanges) ||
        /(?:профил|настройк\w*\s+(?:печати|слайсера)|параметр\w*\s+(?:печати|слайсера)|покажи.*(?:параметр|настройк|профил)|прикреплённ|прикреплены|\.stl)/i
            .test(requestText || '');
}

function formatCardValue(value, unit) {
    if (typeof value === 'boolean') {
        return value ? 'Да' : 'Нет';
    }
    if (typeof value === 'string') {
        const trimmed = value.trim();
        if (!trimmed) {
            return '—';
        }
        return /^-?\d+(?:[.,]\d+)?$/.test(trimmed) ? trimmed + unit : trimmed;
    }
    return formatProfileValue(value, unit);
}

function collectCardRows(data) {
    const rows = [];
    const params = data.params && typeof data.params === 'object' && !Array.isArray(data.params)
        ? data.params : {};
    Object.keys(params).forEach(function (name) {
        const raw = params[name];
        if (raw === null || raw === undefined || typeof raw === 'object') {
            return;
        }
        const key = PARAM_ALIASES[name] || name;
        const meta = PARAM_META[key] || {};
        rows.push({
            key: key,
            raw: raw,
            label: meta.label || name.replace(/_/g, ' '),
            group: meta.group || 'Прочее',
            value: formatCardValue(raw, meta.unit || '')
        });
    });
    if (Array.isArray(data.rows)) {
        data.rows.forEach(function (row) {
            if (row && row.label !== undefined && row.label !== null) {
                rows.push({
                    key: null,
                    raw: row.value,
                    label: String(row.label),
                    group: String(row.group || 'Прочее'),
                    value: formatCardValue(row.value, row.unit || '')
                });
            }
        });
    }
    return rows;
}

function buildCardSettings(card, rows) {
    const container = card.querySelector('.profile-settings');
    if (!container) {
        return;
    }
    while (container.firstChild) {
        container.removeChild(container.firstChild);
    }
    if (!rows.length) {
        const empty = document.createElement('p');
        empty.className = 'empty-state';
        empty.textContent = 'Параметры не переданы.';
        container.appendChild(empty);
        return;
    }
    const groups = new Map();
    rows.forEach(function (row) {
        if (!groups.has(row.group)) {
            groups.set(row.group, []);
        }
        groups.get(row.group).push(row);
    });
    const names = Array.from(groups.keys()).sort(function (a, b) {
        const ia = GROUP_ORDER.indexOf(a);
        const ib = GROUP_ORDER.indexOf(b);
        return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib);
    });
    names.forEach(function (name) {
        const section = document.createElement('section');
        section.className = 'parameter-group';
        const heading = document.createElement('h3');
        heading.textContent = name;
        const list = document.createElement('dl');
        list.className = 'parameter-list';
        groups.get(name).forEach(function (row) {
            const line = document.createElement('div');
            line.className = 'parameter-row';
            const term = document.createElement('dt');
            term.textContent = row.label;
            const value = document.createElement('dd');
            value.textContent = row.value;
            line.appendChild(term);
            line.appendChild(value);
            list.appendChild(line);
        });
        section.appendChild(heading);
        section.appendChild(list);
        container.appendChild(section);
    });
}

function baseName(name) {
    return String(name || '').split(/[\\/]/).pop();
}

function pickModelFile(name, files) {
    if (name) {
        const found = uploadedModels.get(baseName(name).toLowerCase());
        if (found) {
            return found;
        }
    }
    const list = files && files.length ? files : lastUploadedModels;
    return list.length ? list[0] : null;
}

/* ---------- STL: разбор и подготовка сетки ---------- */

function parseSTL(buffer) {
    const size = buffer.byteLength;
    let positions = null;
    if (size >= 84) {
        const view = new DataView(buffer);
        const count = view.getUint32(80, true);
        if (count > 0 && 84 + count * 50 === size) {
            positions = new Float32Array(count * 9);
            let offset = 84;
            for (let i = 0; i < count; i += 1) {
                offset += 12;
                for (let k = 0; k < 9; k += 1) {
                    positions[i * 9 + k] = view.getFloat32(offset, true);
                    offset += 4;
                }
                offset += 2;
            }
        }
    }
    if (!positions) {
        const text = new TextDecoder('utf-8').decode(buffer);
        const pattern = /vertex\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)/g;
        const values = [];
        let match;
        while ((match = pattern.exec(text)) !== null) {
            values.push(parseFloat(match[1]), parseFloat(match[2]), parseFloat(match[3]));
        }
        if (values.length < 9 || values.length % 9) {
            throw new Error('файл не похож на STL');
        }
        positions = Float32Array.from(values);
    }
    return positions;
}

function prepareMesh(source) {
    let triangles = source.length / 9;
    const stride = triangles > MAX_PREVIEW_TRIANGLES ? Math.ceil(triangles / MAX_PREVIEW_TRIANGLES) : 1;
    const kept = Math.ceil(triangles / stride);
    const positions = new Float32Array(kept * 9);
    let n = 0;
    for (let i = 0; i < triangles; i += stride) {
        for (let k = 0; k < 9; k += 1) {
            positions[n * 9 + k] = source[i * 9 + k];
        }
        n += 1;
    }
    triangles = n;

    const min = [Infinity, Infinity, Infinity];
    const max = [-Infinity, -Infinity, -Infinity];
    for (let i = 0; i < triangles * 3; i += 1) {
        for (let a = 0; a < 3; a += 1) {
            const v = positions[i * 3 + a];
            if (!Number.isFinite(v)) {
                throw new Error('в модели есть нечисловые координаты');
            }
            if (v < min[a]) { min[a] = v; }
            if (v > max[a]) { max[a] = v; }
        }
    }
    const center = [(min[0] + max[0]) / 2, (min[1] + max[1]) / 2, (min[2] + max[2]) / 2];
    let radius = 0;
    for (let i = 0; i < triangles * 3; i += 1) {
        const dx = positions[i * 3] - center[0];
        const dy = positions[i * 3 + 1] - center[1];
        const dz = positions[i * 3 + 2] - center[2];
        radius = Math.max(radius, Math.sqrt(dx * dx + dy * dy + dz * dz));
    }
    radius = radius || 1;

    const normals = new Float32Array(triangles * 9);
    for (let i = 0; i < triangles; i += 1) {
        const o = i * 9;
        for (let k = 0; k < 9; k += 3) {
            positions[o + k] = (positions[o + k] - center[0]) / radius;
            positions[o + k + 1] = (positions[o + k + 1] - center[1]) / radius;
            positions[o + k + 2] = (positions[o + k + 2] - center[2]) / radius;
        }
        const ux = positions[o + 3] - positions[o];
        const uy = positions[o + 4] - positions[o + 1];
        const uz = positions[o + 5] - positions[o + 2];
        const vx = positions[o + 6] - positions[o];
        const vy = positions[o + 7] - positions[o + 1];
        const vz = positions[o + 8] - positions[o + 2];
        let nx = uy * vz - uz * vy;
        let ny = uz * vx - ux * vz;
        let nz = ux * vy - uy * vx;
        const length = Math.sqrt(nx * nx + ny * ny + nz * nz) || 1;
        nx /= length; ny /= length; nz /= length;
        for (let k = 0; k < 9; k += 3) {
            normals[o + k] = nx;
            normals[o + k + 1] = ny;
            normals[o + k + 2] = nz;
        }
    }
    return {
        positions: positions,
        normals: normals,
        vertexCount: triangles * 3,
        size: { x: max[0] - min[0], y: max[1] - min[1], z: max[2] - min[2] }
    };
}

/* ---------- Минимальный WebGL-просмотрщик без внешних библиотек ---------- */

function mat4Multiply(a, b) {
    const out = new Float32Array(16);
    for (let c = 0; c < 4; c += 1) {
        for (let r = 0; r < 4; r += 1) {
            let sum = 0;
            for (let k = 0; k < 4; k += 1) {
                sum += a[k * 4 + r] * b[c * 4 + k];
            }
            out[c * 4 + r] = sum;
        }
    }
    return out;
}

function mat4RotateX(angle) {
    const c = Math.cos(angle);
    const s = Math.sin(angle);
    return new Float32Array([1, 0, 0, 0, 0, c, s, 0, 0, -s, c, 0, 0, 0, 0, 1]);
}

function mat4RotateY(angle) {
    const c = Math.cos(angle);
    const s = Math.sin(angle);
    return new Float32Array([c, 0, -s, 0, 0, 1, 0, 0, s, 0, c, 0, 0, 0, 0, 1]);
}

function mat4Perspective(fovy, aspect, near, far) {
    const f = 1 / Math.tan(fovy / 2);
    return new Float32Array([
        f / aspect, 0, 0, 0,
        0, f, 0, 0,
        0, 0, (far + near) / (near - far), -1,
        0, 0, 2 * far * near / (near - far), 0
    ]);
}

function createModelViewer(canvas, mesh) {
    const options = { antialias: true, alpha: true };
    const gl = canvas.getContext('webgl', options) || canvas.getContext('experimental-webgl', options);
    if (!gl) {
        return null;
    }

    function compile(type, source) {
        const shader = gl.createShader(type);
        gl.shaderSource(shader, source);
        gl.compileShader(shader);
        if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
            throw new Error(gl.getShaderInfoLog(shader) || 'ошибка шейдера');
        }
        return shader;
    }

    const program = gl.createProgram();
    gl.attachShader(program, compile(gl.VERTEX_SHADER,
        'attribute vec3 aPos;attribute vec3 aNorm;uniform mat4 uMVP;uniform mat4 uRot;varying vec3 vN;' +
        'void main(){vN=(uRot*vec4(aNorm,0.0)).xyz;gl_Position=uMVP*vec4(aPos,1.0);}'));
    gl.attachShader(program, compile(gl.FRAGMENT_SHADER,
        'precision mediump float;varying vec3 vN;' +
        'void main(){vec3 n=normalize(vN);' +
        'float key=dot(n,normalize(vec3(-0.45,0.75,0.6)))*0.5+0.5;' +
        'float fill=dot(n,normalize(vec3(0.7,-0.3,0.4)))*0.5+0.5;' +
        'vec3 base=vec3(0.50,0.72,0.80);' +
        'vec3 color=base*(0.32+0.62*key*key+0.18*fill);' +
        'gl_FragColor=vec4(color,1.0);}'));
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
        throw new Error('не удалось собрать шейдеры');
    }
    gl.useProgram(program);

    function bindBuffer(data, name) {
        const buffer = gl.createBuffer();
        gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
        gl.bufferData(gl.ARRAY_BUFFER, data, gl.STATIC_DRAW);
        const location = gl.getAttribLocation(program, name);
        gl.enableVertexAttribArray(location);
        gl.vertexAttribPointer(location, 3, gl.FLOAT, false, 0, 0);
        return buffer;
    }
    const positionBuffer = bindBuffer(mesh.positions, 'aPos');
    const normalBuffer = bindBuffer(mesh.normals, 'aNorm');
    const mvpLocation = gl.getUniformLocation(program, 'uMVP');
    const rotLocation = gl.getUniformLocation(program, 'uRot');
    gl.enable(gl.DEPTH_TEST);
    gl.clearColor(0, 0, 0, 0);

    const reduceMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    const zUp = mat4RotateX(-Math.PI / 2);
    let yaw = -0.6;
    let pitch = 0.5;
    let hovering = false;
    let dragging = false;
    let frame = 0;
    let lastTime = 0;
    let lastX = 0;
    let lastY = 0;
    let disposed = false;

    function resize() {
        const ratio = Math.min(window.devicePixelRatio || 1, 2);
        const width = Math.round(canvas.clientWidth * ratio);
        const height = Math.round(canvas.clientHeight * ratio);
        if (width > 0 && height > 0 && (canvas.width !== width || canvas.height !== height)) {
            canvas.width = width;
            canvas.height = height;
        }
    }

    function draw() {
        if (disposed) {
            return;
        }
        resize();
        if (!canvas.width || !canvas.height) {
            return;
        }
        gl.viewport(0, 0, canvas.width, canvas.height);
        gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
        const aspect = canvas.width / canvas.height;
        const distance = 3.9 / Math.min(1, aspect);
        const rotation = mat4Multiply(mat4RotateX(pitch), mat4Multiply(mat4RotateY(yaw), zUp));
        const move = new Float32Array([1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, -distance, 1]);
        const mvp = mat4Multiply(mat4Perspective(0.56, aspect, 0.1, 50),
            mat4Multiply(move, rotation));
        gl.uniformMatrix4fv(mvpLocation, false, mvp);
        gl.uniformMatrix4fv(rotLocation, false, rotation);
        gl.drawArrays(gl.TRIANGLES, 0, mesh.vertexCount);
    }

    function tick(time) {
        frame = 0;
        if (disposed) {
            return;
        }
        const delta = lastTime ? Math.min(0.05, (time - lastTime) / 1000) : 0;
        lastTime = time;
        if (hovering && !dragging && !reduceMotion) {
            yaw += delta * 0.9;
        }
        draw();
        if (hovering || dragging) {
            frame = requestAnimationFrame(tick);
        } else {
            lastTime = 0;
        }
    }

    function start() {
        if (!frame && !disposed) {
            frame = requestAnimationFrame(tick);
        }
    }

    function stopDrag() {
        dragging = false;
        canvas.classList.remove('is-grabbing');
    }

    canvas.addEventListener('pointerenter', function () { hovering = true; start(); });
    canvas.addEventListener('pointerleave', function () { hovering = false; });
    canvas.addEventListener('pointerdown', function (event) {
        dragging = true;
        lastX = event.clientX;
        lastY = event.clientY;
        canvas.classList.add('is-grabbing');
        if (canvas.setPointerCapture) {
            canvas.setPointerCapture(event.pointerId);
        }
        start();
    });
    canvas.addEventListener('pointermove', function (event) {
        if (!dragging) {
            return;
        }
        yaw += (event.clientX - lastX) * 0.01;
        pitch = Math.max(-1.5, Math.min(1.5, pitch + (event.clientY - lastY) * 0.01));
        lastX = event.clientX;
        lastY = event.clientY;
    });
    canvas.addEventListener('pointerup', stopDrag);
    canvas.addEventListener('pointercancel', stopDrag);
    canvas.addEventListener('dblclick', function () {
        yaw = -0.6;
        pitch = 0.5;
        draw();
    });

    let observer = null;
    if (window.ResizeObserver) {
        observer = new ResizeObserver(draw);
        observer.observe(canvas);
    }
    draw();

    return {
        dispose: function () {
            disposed = true;
            if (frame) {
                cancelAnimationFrame(frame);
            }
            if (observer) {
                observer.disconnect();
            }
            gl.deleteBuffer(positionBuffer);
            gl.deleteBuffer(normalBuffer);
            const lose = gl.getExtension('WEBGL_lose_context');
            if (lose) {
                lose.loseContext();
            }
        }
    };
}

async function loadModelIntoCard(card, source) {
    const stage = card.querySelector('.model-stage');
    const canvas = card.querySelector('.model-canvas');
    const empty = card.querySelector('[data-model-empty]');
    const sizeElement = card.querySelector('[data-model-size]');
    if (!stage || !canvas || !empty) {
        return;
    }
    empty.hidden = false;
    empty.textContent = 'Загрузка модели…';
    try {
        let buffer;
        if (typeof source === 'string') {
            if (source.charAt(0) !== '/') {
                throw new Error('разрешены только пути на этом сервере');
            }
            const response = await fetch(source);
            if (!response.ok) {
                throw new Error('HTTP ' + response.status);
            }
            buffer = await response.arrayBuffer();
        } else {
            buffer = await source.arrayBuffer();
        }
        const mesh = prepareMesh(parseSTL(buffer));
        if (!card.isConnected) {
            return;
        }
        const viewer = createModelViewer(canvas, mesh);
        if (!viewer) {
            empty.textContent = 'WebGL недоступен: 3D-превью не показано.';
            return;
        }
        activeViewers.push(viewer);
        while (activeViewers.length > MAX_VIEWERS) {
            activeViewers.shift().dispose();
        }
        stage.classList.add('has-model');
        empty.hidden = true;
        if (sizeElement) {
            sizeElement.textContent = 'Размеры: ' + [mesh.size.x, mesh.size.y, mesh.size.z]
                .map(function (v) { return formatProfileValue(v, ''); }).join(' × ') + ' мм';
        }
    } catch (error) {
        empty.hidden = false;
        empty.textContent = 'Не удалось показать модель: ' + error.message;
    }
}

/* ---------- Сборка карточки ---------- */

function fillProposalCard(card, data, rows) {
    const eyebrow = card.querySelector('[data-profile-eyebrow]');
    if (eyebrow) {
        eyebrow.textContent = String(data.eyebrow || 'Рекомендованный профиль').toUpperCase();
    }
    const title = card.querySelector('[data-profile-title]');
    if (title) {
        title.textContent = data.title || 'Стартовый профиль';
    }
    const modelName = card.querySelector('[data-model-name]');
    const rawName = data.model_name || data.model || data.name || '';
    if (modelName) {
        modelName.textContent = rawName ? baseName(rawName).replace(/\.stl$/i, '') : 'Модель';
    }
    const note = card.querySelector('[data-model-note]');
    if (note && data.conditions) {
        note.textContent = String(data.conditions);
        note.hidden = false;
    }
    const warning = card.querySelector('[data-profile-note]');
    if (warning) {
        warning.textContent = data.warning || 'Предварительный профиль: требует калибровки перед печатью.';
    }
    buildCardSettings(card, rows);
}

function setupApplyButton(card, data, rows) {
    const button = card.querySelector('.apply-profile-button');
    if (!button) {
        return;
    }
    const lines = rows.filter(function (row) { return row.key; })
        .map(function (row) { return row.key + ' = ' + row.raw; });
    if (!lines.length) {
        button.hidden = true;
        return;
    }
    button.addEventListener('click', function () {
        if (isSending) {
            statusText.textContent = 'Дождитесь завершения текущего запроса.';
            return;
        }
        submitMessage(
            'Применить профиль «' + (data.title || 'стартовый профиль') + '» в слайсере:\n' +
            lines.join('\n') + '\nПрименяй материал и параметры печати как указано.',
            []
        );
    });
}

function appendProfileCard(messageElement, cardData, modelFile) {
    if (!profileCardTemplate || !profileCardTemplate.content.firstElementChild) {
        return null;
    }
    const card = profileCardTemplate.content.firstElementChild.cloneNode(true);
    profileCardSequence += 1;
    const ids = new Map();
    card.querySelectorAll('[id]').forEach(function (element) {
        const uniqueId = element.id + '-' + profileCardSequence;
        ids.set(element.id, uniqueId);
        element.id = uniqueId;
    });
    ['aria-labelledby', 'aria-describedby', 'aria-controls'].forEach(function (attribute) {
        [card].concat(Array.from(card.querySelectorAll('[' + attribute + ']'))).forEach(function (element) {
            const value = element.getAttribute(attribute);
            if (value) {
                element.setAttribute(attribute, value.split(/\s+/).map(function (id) {
                    return ids.get(id) || id;
                }).join(' '));
            }
        });
    });

    const file = modelFile === undefined ? pickModelFile(null, null) : modelFile;
    let source = file || null;

    if (cardData) {
        const rows = collectCardRows(cardData);
        card.dataset.live = 'false';
        fillProposalCard(card, cardData, rows);
        setupApplyButton(card, cardData, rows);
        if (!source && typeof cardData.model_url === 'string') {
            source = cardData.model_url;
        }
    } else {
        card.dataset.live = 'true';
        const applyButton = card.querySelector('.apply-profile-button');
        if (applyButton) {
            applyButton.hidden = true;
        }
        const warning = card.querySelector('[data-profile-note]');
        if (warning) {
            warning.textContent = 'Текущие значения из слайсера.';
        }
    }

    if (file) {
        card.dataset.localModel = 'true';
        const name = card.querySelector('[data-model-name]');
        if (name && (!cardData || !name.textContent || name.textContent === 'Модель')) {
            name.textContent = file.name.replace(/\.stl$/i, '');
        }
    }

    const container = document.createElement('div');
    container.className = 'message-profile';
    container.appendChild(card);
    const body = messageElement.querySelector('.message-body');
    if (body) {
        body.after(container);
    } else {
        messageElement.appendChild(container);
    }
    if (!cardData) {
        // «Живая» карточка появится, только когда слайсер вернёт данные.
        container.hidden = true;
    }

    if (source) {
        loadModelIntoCard(card, source);
    }
    return card;
}

function addMessage(author, text, type, attachments, requestText, hasProfileChanges, modelFiles) {
    const messageElement = document.createElement('article');
    messageElement.className = 'message ' + type;

    const authorElement = document.createElement('span');
    authorElement.className = 'message-author';
    authorElement.textContent = author;

    const bodyElement = document.createElement('div');
    bodyElement.className = 'message-body';

    let cardData = null;
    let wantsLiveCard = false;
    if (type === 'assistant') {
        const parsed = extractCard(text);
        cardData = parsed.card;
        const bodyText = parsed.rest;
        wantsLiveCard = !cardData && shouldShowProfileCard(text, requestText || '', hasProfileChanges);

        if (!bodyText.trim()) {
            bodyElement.hidden = true;
        } else {
            formatAssistantMessage(bodyElement, bodyText);
        }
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

    messageList.appendChild(messageElement);

    if (cardData) {
        const nameHint = cardData.model_name || cardData.model || cardData.name;
        appendProfileCard(messageElement, cardData, pickModelFile(nameHint, modelFiles));
    } else if (wantsLiveCard) {
        appendProfileCard(messageElement, null, pickModelFile(null, modelFiles));
    }

    if (type === 'assistant') {
        lastAssistantMessageElement = messageElement;
    }
    if (type === 'assistant' && (cardData || wantsLiveCard)) {
        // Ответ высокий: показываем его с начала, а не с конца.
        messageList.scrollTop = Math.max(0, messageElement.offsetTop - messageList.offsetTop - 12);
    } else {
        messageList.scrollTop = messageList.scrollHeight;
    }
    return messageElement;
}

function finalizeLiveCard(card) {
    const container = card.closest('.message-profile');
    let visibleRows = 0;
    card.querySelectorAll('.parameter-row').forEach(function (row) {
        const value = row.querySelector('dd');
        const empty = !value || value.textContent.trim() === '—';
        row.hidden = empty;
        if (!empty) {
            visibleRows += 1;
        }
    });
    card.querySelectorAll('.parameter-group').forEach(function (group) {
        group.hidden = !group.querySelector('.parameter-row:not([hidden])');
    });
    let note = card.querySelector('.profile-empty');
    if (!note) {
        note = document.createElement('p');
        note.className = 'empty-state profile-empty';
        note.textContent = 'Параметры слайсера пока недоступны.';
        const settings = card.querySelector('.profile-settings');
        if (settings) {
            settings.appendChild(note);
        }
    }
    note.hidden = visibleRows > 0;
    const hasModel = card.dataset.localModel === 'true';
    if (container) {
        container.hidden = !(visibleRows > 0 || hasModel);
    }
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

            document.querySelectorAll('.slice-profile[data-live="true"]').forEach(function (profileCard) {
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
                const localModel = profileCard.dataset.localModel === 'true';
                if (modelName && !localModel) {
                    modelName.textContent = model ? model.name : 'Модель не загружена';
                }
                if (modelSize && !localModel) {
                    const size = model && model.size_mm;
                    modelSize.textContent = size
                        ? 'Размеры: ' + ['x', 'y', 'z'].map(function (axis) {
                            return formatProfileValue(size[axis], ' мм');
                        }).join(' × ')
                        : 'Размеры появятся после загрузки модели';
                }

                const materialCell = profileCard.querySelector('[data-profile-material]');
                if (materialCell && materialCell.textContent.trim() === '') {
                    materialCell.textContent = '—';
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
                finalizeLiveCard(profileCard);
            });
        })
        .catch(function (error) {
            document.querySelectorAll('.slice-profile[data-live="true"]').forEach(finalizeLiveCard);
            statusText.textContent = 'Профиль слайсера недоступен (' + error.message + '). Карточки с данными ИИ работают без него.';
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

function readClearMarker() {
    try {
        return localStorage.getItem(CLEAR_MARKER_KEY);
    } catch (error) {
        return null;
    }
}

function writeClearMarker(value) {
    try {
        if (value === null) {
            localStorage.removeItem(CLEAR_MARKER_KEY);
        } else {
            localStorage.setItem(CLEAR_MARKER_KEY, value);
        }
    } catch (error) {
        /* без localStorage чат просто покажет историю заново */
    }
}

async function clearChat() {
    if (isSending) {
        statusText.textContent = 'Дождитесь ответа, затем очистите чат.';
        return;
    }
    try {
        const response = await fetch('/api/history');
        if (response.ok) {
            const data = await response.json();
            const list = Array.isArray(data.messages) ? data.messages : [];
            writeClearMarker(list.length ? JSON.stringify({
                role: list[list.length - 1].role,
                content: list[list.length - 1].content,
                count: list.length
            }) : null);
        }
    } catch (error) {
        /* сервер недоступен: очищаем только экран */
    }
    activeViewers.splice(0).forEach(function (viewer) { viewer.dispose(); });
    uploadedModels.clear();
    lastUploadedModels = [];
    lastAssistantMessageElement = null;
    Array.prototype.slice.call(messageList.children).forEach(function (child) {
        if (child !== profileCardTemplate) {
            messageList.removeChild(child);
        }
    });
    while (activityList.firstChild) {
        activityList.removeChild(activityList.firstChild);
    }
    activityEmpty.hidden = false;
    selectedFiles = [];
    renderAttachments();
    statusText.textContent = 'Чат очищен.';
    messageInput.focus();
}

if (clearChatButton) {
    clearChatButton.addEventListener('click', clearChat);
}

async function loadConversation() {
    try {
        const response = await fetch('/api/history');
        if (!response.ok) {
            throw new Error('HTTP ' + response.status);
        }
        const data = await response.json();
        if (Array.isArray(data.messages)) {
            let visibleMessages = data.messages;
            const markerText = readClearMarker();
            if (markerText) {
                const marker = tryParseJson(markerText);
                if (marker) {
                    for (let i = data.messages.length - 1; i >= 0; i -= 1) {
                        const item = data.messages[i];
                        if (item && item.role === marker.role && item.content === marker.content) {
                            visibleMessages = data.messages.slice(i + 1);
                            break;
                        }
                    }
                }
            }
            let lastUserText = '';
            visibleMessages.forEach(function (message) {
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

async function submitMessage(messageText, messageAttachments) {
    if (isSending) {
        return;
    }
    messageAttachments = messageAttachments || [];
    messageAttachments.forEach(function (file) {
        uploadedModels.set(baseName(file.name).toLowerCase(), file);
    });
    if (messageAttachments.length) {
        lastUploadedModels = messageAttachments.slice();
    }

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
            pendingProfileChanges || messageAttachments.length > 0,
            messageAttachments
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

function sendMessage(event) {
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
    return submitMessage(messageText, messageAttachments);
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
