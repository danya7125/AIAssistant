import json
import os
import ssl

from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


class AgentClient:
    def __init__(self, settings_tools):
        self.tools = settings_tools

    def _request(self, config, messages):
        body = json.dumps({'model': config['model'], 'messages': messages,
                           'tools': self.tools.schemas(), 'tool_choice': 'auto'},
                          ensure_ascii=False, allow_nan=False).encode('utf-8')
        request = Request(config['url'].rstrip('/') + '/chat/completions', data=body,
                          headers={'Authorization': 'Bearer ' + config['key'],
                                   'Content-Type': 'application/json'}, method='POST')
        context = ssl.create_default_context()
        ca_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cacert.pem')
        if os.path.isfile(ca_file):
            # Keep OS trust and add the bundled Mozilla roots.
            context.load_verify_locations(cafile=ca_file)
        try:
            with urlopen(request, timeout=350, context=context) as response:
                result = json.loads(response.read().decode('utf-8'))
            message = result['choices'][0]['message']
            if not isinstance(message, dict):
                raise ValueError('Неверный формат ответа API.')
            return message
        except HTTPError as error:
            hints = {400: 'Проверьте модель: она должна поддерживать tools / tool calling.',
                     401: 'Проверьте API-ключ.', 403: 'Нет доступа к модели.',
                     404: 'Проверьте URL и модель.', 429: 'Лимит запросов или квота.',
                     503: 'Сервер ИИ временно перегружен (ошибка 503). Повторите запрос через минуту.'}
            raise RuntimeError('Ошибка API {0}. {1}'.format(error.code, hints.get(error.code, 'Повторите позже.')))
        except URLError as error:
            raise RuntimeError('Ошибка подключения к API: ' + str(error.reason))

    def run(self, config, text, history, print_settings=None, progress=None):
        def notify(event):
            if progress is not None:
                progress(event)

        messages = [{'role': 'system', 'content': (
            'Ты — узкоспециализированный технический помощник внутри STE Slicer для 3D/5D-печати. '
            'Отвечай строго по-русски.\n\n'
            'ЖЕСТКИЕ ПРАВИЛА БЕЗОПАСНОСТИ И ТЕМАТИКИ:\n'
            '1. Отвечай ТОЛЬКО на вопросы, связанные с 3D/5D-печатью, STE Slicer, материалами, '
            'профилями, поддержками, слайсингом и аддитивными технологиями.\n'
            '2. Если вопрос НЕ относится к 3D/5D-печати или STE Slicer (например: оружие, политика, '
            'кулинария, рецепты, погода, бытовые темы), КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО давать советы, '
            'рецепты или рассуждать на эту тему. В таком случае отвечай строго одной фразой: '
            '«Я — помощник по STE Slicer и 3D/5D-печати. Я отвечаю только на вопросы по настройке печати и работе со слайсером.»\n\n'
            'ПРАВИЛА РАБОТЫ С ИНСТРУМЕНТАМИ:\n'
            'Ты — технический помощник внутри STE Slicer для 3D/5D-печати. Отвечай строго по-русски.\n\n'
            'У тебя есть инструменты для чтения/изменения настроек (set_print_settings) и запуска нарезки (start_slicing).\n'
            '1. Когда пользователь просит подготовить деталь (например, сделать прочный кронштейн), примени нужные параметры через set_print_settings.\n'
            '2. После применения настроек обязательно вызови инструмент start_slicing, чтобы запустить процесс нарезки и генерации G-code.\n'
            '3. После успешного выполнения напиши пользователю краткий отчет: какие параметры изменены и что модель успешно нарезана для печати.'
        )}]
        if print_settings is not None:
            messages.append({'role': 'system', 'content': 'Снимок основных настроек: ' +
                             json.dumps(print_settings, ensure_ascii=False)})
        messages.extend(history[-20:])
        messages.append({'role': 'user', 'content': text})
        successful_changes = []
        try:
            for step in range(15):
                message = self._request(config, messages)
                calls = message.get('tool_calls') or []
                if not calls:
                    reply = message.get('content')
                    if not isinstance(reply, str) or not reply.strip():
                        raise ValueError('API вернул пустой ответ.')
                    return reply
                if not isinstance(calls, list) or len(calls) > 10:
                    raise ValueError('API вернул слишком много вызовов инструментов.')
                # Validate identifiers before executing any tool in this response.
                ids = set()
                for call in calls:
                    if (not isinstance(call, dict) or not isinstance(call.get('id'), str)
                            or call['id'] in ids or call.get('type') != 'function'):
                        raise ValueError('API вернул неверный вызов инструмента.')
                    ids.add(call['id'])
                messages.append({'role': 'assistant', 'content': message.get('content'),
                                 'tool_calls': calls})
                for call in calls:
                    function = call.get('function', {})
                    name = function.get('name', '')
                    try:
                        raw_arguments = function.get('arguments', '{}')
                        if not isinstance(raw_arguments, str) or len(raw_arguments) > 20000:
                            raise ValueError('Неверный размер/формат аргументов.')
                        arguments = json.loads(raw_arguments)
                        notify({'phase': 'start', 'name': name, 'arguments': arguments})
                        output = self.tools.call(name, arguments)
                        result = {'ok': True, 'result': output}
                        if name == 'set_print_settings':
                            successful_changes.extend(output.get('changes', []))
                    except Exception as error:
                        result = {'ok': False, 'error': str(error).replace(config['key'], '[hidden]')}
                    notify({'phase': 'result', 'name': name, 'output': result})
                    messages.append({'role': 'tool', 'tool_call_id': call['id'],
                                     'content': json.dumps(result, ensure_ascii=False, allow_nan=False)})
            raise RuntimeError('Достигнут предел: 8 шагов агента. Уточните запрос.')
        except Exception as error:
            message = str(error).replace(config['key'], '[hidden]')
            if successful_changes:
                message += '\nУже применены изменения:\n' + '\n'.join(
                    '{0}: {1} → {2}'.format(c['key'], c['before'], c['after'])
                    for c in successful_changes)
            return message
