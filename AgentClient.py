import copy
import http.client
import ast
import json
import os
import queue
import re
import ssl
import threading
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


class AgentClient:
    RETRY_DELAYS = (2, 5)
    MAX_STEPS = 12
    TASK_TIMEOUT = 1800
    REQUEST_TIMEOUT = 600

    def __init__(self, settings_tools):
        self.tools = settings_tools
        self._transport_attempts = 0

    def _request(self, config, messages, timeout, include_diagnostics=False):
        last_content = messages[-1].get('content', '') if messages else ''
        is_final = isinstance(last_content, str) and last_content.startswith('[FINAL_RESPONSE]')
        payload = {
            'model': config['model'],
            'messages': self._compact_messages(messages),
            'temperature': 0.1,
            'enable_thinking': True
        }
        if is_final:
            payload['tool_choice'] = 'none'
        else:
            try:
                payload['tools'] = self.tools.schemas(include_diagnostics=include_diagnostics)
            except TypeError:
                payload['tools'] = self.tools.schemas()
            payload['tool_choice'] = 'auto'

        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
        request = Request(
            config['url'].rstrip('/') + '/chat/completions',
            data=body,
            headers={
                'Authorization': 'Bearer ' + config['key'],
                'Content-Type': 'application/json'
            },
            method='POST'
        )
        context = ssl.create_default_context()
        ca_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cacert.pem')
        if os.path.isfile(ca_file):
            context.load_verify_locations(cafile=ca_file)
        try:
            with urlopen(request, timeout=timeout, context=context) as response:
                raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError('Ответ API слишком большой.')
            result = json.loads(raw.decode('utf-8'))
            choice = result['choices'][0]
            if choice.get('finish_reason') == 'length':
                raise RuntimeError('API обрезал ответ по лимиту модели. Сократите запрос.')
            message = choice['message']
            if not isinstance(message, dict):
                raise ValueError('Неверный формат ответа API.')
            return message
        except HTTPError as error:
            hints = {
                400: 'Проверьте поддержку tool calling у модели.',
                401: 'Проверьте ключ.',
                403: 'Нет доступа к модели.',
                404: 'Проверьте URL и модель.',
                429: 'Лимит запросов или квота.',
                503: 'API временно недоступен.'
            }
            raise RuntimeError('Ошибка API {0}. {1}'.format(error.code, hints.get(error.code, 'Повторите позже.'))) from error
        except URLError as error:
            raise RuntimeError('Ошибка подключения к API: ' + str(error.reason)) from error

    def _check(self, cancel, deadline):
        if cancel.is_set():
            raise RuntimeError('Запрос отменён пользователем.')
        if time.monotonic() >= deadline:
            raise RuntimeError('Истёк общий лимит задачи: {0} секунд.'.format(self.TASK_TIMEOUT))

    @staticmethod
    def _compact_messages(messages):
        # Keep full local history. Only shrink derived orientation tables on the wire.
        result = copy.deepcopy(messages)
        for msg in result:
            if msg.get('role') != 'tool' or not isinstance(msg.get('content'), str):
                continue
            try:
                obj = json.loads(msg['content'])
                report = obj.get('result', {})
                candidates = report.get('candidates')
                if not isinstance(candidates,list) or len(candidates)<=10:
                    continue
                selected = [c for c in candidates if c.get('pareto_candidate') or c.get('degrees')==0]
                for c in candidates[:3]:
                    if c not in selected:
                        selected.append(c)
                # Never drop Pareto tradeoffs or the current orientation.
                if len(selected)<len(candidates):
                    report['candidates'] = selected
                    report['omitted_dominated_candidates'] = len(candidates)-len(selected)
                    report['selection_note'] = 'Переданы все Pareto-варианты, текущее положение и первые три варианта. Остальные доминируемые строки опущены; ограничения расчётов сохранены.'
                    msg['content'] = json.dumps(obj,ensure_ascii=False,allow_nan=False)
            except (ValueError,TypeError,AttributeError):
                continue
        return result

    @staticmethod
    def _retryable(error):
        seen = set()
        current = error
        temporary = False
        while isinstance(current, BaseException) and id(current) not in seen:
            seen.add(id(current))
            if isinstance(current, ssl.SSLError):
                return False  # Certificate/protocol errors need diagnosis, not retries.
            if isinstance(current, HTTPError):
                return current.code in (502,503,504)
            if isinstance(current, (ConnectionResetError, ConnectionAbortedError,
                                    BrokenPipeError, TimeoutError,
                                    http.client.IncompleteRead, http.client.RemoteDisconnected)):
                temporary = True
            if isinstance(current, OSError) and (getattr(current,'winerror',None) in (10053,10054,10060) or getattr(current,'errno',None) in (104,103,110,32,10053,10054,10060)):
                temporary = True
            reason = getattr(current,'reason',None)
            current = reason if isinstance(reason,BaseException) else current.__cause__
        return temporary

    def _bounded_request(self, config, messages, cancel, deadline, include_diagnostics=False, notify=None):
        frozen_messages = copy.deepcopy(messages)
        for attempt in range(len(self.RETRY_DELAYS)+1):
            self._check(cancel,deadline)
            self._transport_attempts += 1
            try:
                return self._bounded_request_once(config, frozen_messages, cancel, deadline, include_diagnostics)
            except Exception as error:
                if not self._retryable(error) or attempt == len(self.RETRY_DELAYS):
                    raise
                self._check(cancel,deadline)
                delay = self.RETRY_DELAYS[attempt]
                if deadline-time.monotonic() <= delay:
                    raise
                if notify is not None:
                    notify({'phase':'status','message':'Временный сбой API. Повтор {0}/2 через {1} с. Выполненные действия не повторяются.'.format(attempt+1,delay)})
                if cancel.wait(delay):
                    self._check(cancel,deadline)
        raise RuntimeError('Повторные попытки API исчерпаны.')

    def _bounded_request_once(self, config, messages, cancel, deadline, include_diagnostics=False):
        self._check(cancel, deadline)
        timeout = min(self.REQUEST_TIMEOUT, max(1, deadline - time.monotonic()))
        result = queue.Queue(1)

        def fetch():
            try:
                result.put((True, self._request(config, messages, timeout, include_diagnostics=include_diagnostics)))
            except Exception as error:
                result.put((False, error))

        worker = threading.Thread(target=fetch)
        worker.daemon = True
        worker.start()
        request_end = min(deadline, time.monotonic() + timeout)
        while True:
            self._check(cancel, deadline)
            if time.monotonic() >= request_end:
                raise RuntimeError('API не ответил за {0} секунд. Запрос остановлен.'.format(int(timeout)))
            try:
                ok, value = result.get(timeout=0.1)
            except queue.Empty:
                continue
            self._check(cancel, deadline)
            if not ok:
                raise value
            return value

    def _summary(self, changes, slicing, reason=None):
        lines = [reason] if reason else []
        if changes:
            lines.append('Проверенные изменения:')
            lines.extend('{0}: {1} → {2}'.format(c['key'], c['before'], c['after']) for c in changes)
        else:
            lines.append('Настройки в этом запросе не изменялись.')
        if slicing and slicing.get('status') == 'completed' and slicing.get('gcode_ready'):
            lines.append('Нарезка завершена; G-code получен в слайсере.')
            estimates = slicing.get('engine_estimates', {})
            if estimates.get('print_time_seconds') is not None:
                lines.append('Время по оценке движка: {0:.1f} мин.'.format(estimates['print_time_seconds']/60))
            if estimates.get('filament_length_m') is not None:
                lines.append('Расход по оценке движка: {0:.3f} м филамента.'.format(estimates['filament_length_m']))
            auto_export = slicing.get('auto_export')
            if auto_export and auto_export.get('ok'):
                lines.append('Автосохранение: G-Code и 3MF успешно сохранены в папку:\n{0}'.format(auto_export.get('target_dir')))
                for sf in auto_export.get('saved_files', []):
                    lines.append('  • {0}'.format(os.path.basename(sf)))
            elif auto_export and not auto_export.get('ok'):
                lines.append('Предупреждение при автосохранении: {0}'.format(auto_export.get('error') or '; '.join(auto_export.get('errors', []))))
        elif slicing:
            lines.append('Нарезка: ' + slicing.get('status', 'не подтверждена'))
            if slicing.get('error'):
                lines.append(slicing['error'])
        else:
            lines.append('Нарезка в этом запросе не запускалась.')
        return '\n'.join(lines)

    @staticmethod
    def _clean_text(text):
        """Удаляет технические теги <think> и <tool_call> из финального текста для пользователя."""
        if not isinstance(text, str):
            return ''
        cleaned = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
        cleaned = re.sub(r'<tool_call>.*?</tool_call>', '', cleaned, flags=re.DOTALL)
        return cleaned.strip()

    @staticmethod
    def _extract_text_tool_calls(content):
        # Ordinary prose, examples and quoted code must never mutate a project.
        return []

    @staticmethod
    def _classify_intent(text, task_text=None, has_models=True):
        t = (text or '').strip().lower()
        readonly = re.search(r'не\s+(?:меня\w*|изменя\w*|применя\w*|трогай)|ничего\s+не|только\s+(?:оцени\w*|покажи\w*|объясни\w*|сравни\w*)', t)
        diagnostic = re.search(r'провер\w*\s+(?:геометр\w*|сетк\w*|модел\w*|g-?code|траектор\w*)', t)
        if readonly:
            return 'diagnostic' if diagnostic else 'question'
        no_slice = re.search(r'без\s+нарез\w*|не\s+(?:нарез\w*|режь|запуска\w*\s+нарез\w*)', t)
        if diagnostic:
            return 'diagnostic'
        action = re.search(r'\b(?:загрузи\w*|открой|положи\w*|поверни\w*|размести\w*|расставь\w*|ориентируй\w*|нарежь|нарезать|подготов\w*|настрой(?:те)?|примени\w*|выстави\w*|установи\w*|поставь\w*|поменяй\w*|смени\w*|включи\w*|выключи\w*|подбери\w*|выбери\w*|запусти\w*)\b', t)
        if re.match(r'^(?:покажи|оцени|сравни|расскажи|объясни|почему|зачем|что|какой|какие|как)\b', t) and not action:
            return 'question'
        if task_text or re.search(r'\bтз\b|техническ\w*\s+задан\w*|условия\s+эксплуатации',t):
            return 'settings_only' if no_slice else 'full_slice_task'
        if action:
            full = re.search(r'нарежь|нарезать|подготов\w*|настрой(?:те)?.*(?:модель|деталь|печать)|запусти\w*.*нарез',t)
            return 'full_slice_task' if full and not no_slice else 'settings_only'
        return 'question'

    def _run_slicing_and_wait(self, cancel, deadline, notify):
        """Запускает нарезку и локально ждёт её завершения (с автоповтором при сбросе таймером слайсера)."""
        notify({'phase': 'start', 'name': 'start_slicing', 'arguments': {}})
        # Даём главному потоку Qt 0.25 с обработать изменения настроек перед стартом слайсера
        cancel.wait(0.25)
        self._check(cancel, deadline)

        slicing = self.tools.call('start_slicing', {}, cancel_event=cancel, deadline=deadline)
        try:
    
            while True:
                while slicing.get('status') in ('starting', 'processing', 'done_signal'):
                    self._check(cancel, deadline)
                    notify({'phase': 'status', 'message': 'Нарезка: {0:.0f}%'.format(slicing.get('progress', 0) * 100)})
                    cancel.wait(0.4)
                    self._check(cancel, deadline)
                    slicing = self.tools.call('get_slicing_status', {}, cancel_event=cancel, deadline=deadline)
    
                if slicing.get('status') == 'completed':
                    break
    
                break
    
            ok_slice = slicing.get('status') == 'completed'
            result = {'ok': ok_slice, 'result': slicing}
            if not ok_slice:
                result['error'] = slicing.get('error') or 'Нарезка не завершилась.'
            notify({'phase': 'result', 'name': 'start_slicing', 'output': result})
            if ok_slice and slicing.get('gcode_ready'):
                notify({'phase': 'start', 'name': 'auto_export', 'arguments': {}})
                try:
                    export_res = self.tools.call('auto_export', {}, cancel_event=cancel, deadline=deadline)
                    slicing['auto_export'] = export_res
                    notify({'phase': 'result', 'name': 'auto_export', 'output': {'ok': export_res.get('ok', False), 'result': export_res}})
                except Exception as ex:
                    slicing['auto_export'] = {'ok': False, 'error': str(ex)}
                    notify({'phase': 'result', 'name': 'auto_export', 'output': {'ok': False, 'error': str(ex)}})
            return slicing, result
        except Exception:
            try:
                self.tools.cancel_slicing()
            except Exception:
                pass
            raise


    def run(self, config, text, history, print_settings=None, progress=None, cancel_event=None, task_text=None):
        cancel = cancel_event or threading.Event()
        deadline = time.monotonic() + self.TASK_TIMEOUT

        def notify(event):
            if progress is not None:
                progress(event)

        messages = [{'role': 'system', 'content': (
            '''Ты ведущий инженер-технолог аддитивного производства и эксперт STE Slicer.
По команде подготовить проект или по ТЗ самостоятельно выбери оптимальный заводской материал Stereotech, настрой параметры и выполни нарезку, без повторных вопросов «уверены?».
Обычный запрос изменить один параметр не разрешает нарезку. Просмотр и сравнение не меняют проект.

СТРОГОЕ ПРАВИЛО ПО КЛЮЧАМ НАСТРОЕК:
НИКОГДА не используй синонимы или придуманные названия параметров. Бери поле key один-в-один из контекста слайсера или list_print_settings.

ПРИОРИТЕТ БРЕНДОВ И ПРОФИЛЕЙ:
Всегда выбирай профили оригинального заводского бренда «Stereotech» (серии Proto, Enduse, Fiberpart, Sealant). Профили бренда «Generic» использовать ЗАПРЕЩЕНО, если в каталоге Stereotech есть материал аналогичной полимерной группы.

ИНЖЕНЕРНАЯ МАТРИЦА СЕМЕЙСТВ МАТЕРИАЛОВ STEREOTECH (wiki.stereotech.org/ru/fdm-materials):
1. Серия «Proto» (Proto PLA): жесткий, низкотемпературный (до +50°C), хрупкий на излом, нестойкий к УФ и воде. Применение: макеты, сувениры, ненагруженные концепт-модели.
2. Серия «Enduse» (ненаполненные конструкционные термопласты):
   • Enduse PETG: вязкий, ударопрочный, не ломается при касании тонких элементов, стойкий к УФ и спиртам, Tраб до +70°C. Применение: фигурки на солнце, корпуса, тара, остекление.
   • Enduse PA6: антифрикционный ненаполненный нейлон, гладкий, износостойкий, Tплавл=220°C (стоек к каплям клея 160°C), стоек к щелочам pH 10-11 и маслам. Идеальная пара сухого скольжения для POM (полиацеталя).
   • Enduse PP: гидрофобный, стоек к щелочам, эластичный, низкий модуль упругости.
   • Enduse ABS / SBS / TPU D60 / TPU D70: прочные детали без волокон.
3. Серия «Fiberpart» (композиты с рубленым волокном - ВЫСОКАЯ ЖЕСТКОСТЬ, АБРАЗИВНЫЙ ИЗНОС ОТВЕТНЫХ ДЕТАЛЕЙ):
   • Индекс «G» (Glass fiber, стекловолокно: PP G30, PA12 G12, PC G20, ABS G13): ДИЭЛЕКТРИК (нет гальванопары с алюминием). Поднимает модуль упругости и термостойкость.
     - Fiberpart PP G30: эталон для крыльчаток насосов в горячем гликоле со щелочью (+85°C, 16 бар).
     - Fiberpart PA12 G12: самый легкий композит (1.05 г/см³), Tраб до +90°C, стоек к маслам и озону, идеален для вентиляторов 3000 об/мин.
     - Fiberpart PC G20: рекордная прочность, Tg=145°C, для силовых шестерен под моменты до 75 Н·м при влажности 98%.
   • ВНИМАНИЕ: Волокна GF/CF на поверхности работают как наждак! КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО ставить Fiberpart (GF/CF) в пары трения скольжения с мягкими материалами (POM, бронза, полимеры) - волокно сточит ответную деталь за смену! Туда ставить ТОЛЬКО ненаполненный Enduse PA6!
4. Серия «Sealant» (эластомеры и уплотнения):
   • Sealant TPU A95 / A90 / A70: высокая эластичность (удлинение >300%), стойкость к маслам, смазкам MoS2, солям и истиранию. Применение: пыльники ШРУС, мембраны, сильфоны, приводные фрикционные ролики и колеса конвейеров.

ПРАВИЛА НАЗНАЧЕНИЯ ГЕОМЕТРИИ (set_print_settings):
• Для тонкостенных эластичных гофр, пыльников, сильфонов и мембран: wall_line_count=2..3 (толщина 0.8..1.2 мм) и infill_sparse_density=0%, иначе деталь потеряет подвижность и заблокирует изгиб!
• Для силовых шестерен, кулачков, колес насосов: wall_line_count=5..8, infill_sparse_density=50..100%, infill_pattern="gyroid", outer_inset_first=True.
• Для чистовых декоративных фигурок без видимых слоев: layer_height=0.10..0.12, speed_print=30..45, outer_inset_first=True.

ПОРЯДОК РАБОТЫ:
При получении ТЗ:
1. Вызови get_scene_models и analyse_orientation для 3D.
2. Проверь базовую плоскость (при наличии плоского фланца/основания ориентируй им на стол, чтобы минимизировать поддержки).
3. Примени точный профиль: set_material(material_id="...").
4. Примени технологический пакет: set_print_settings(values={...}).
5. Запусти start_slicing().
В кратком итоге укажи: выбранный материал Stereotech, ключевые настройки и обоснование отсева по паспорту wiki.stereotech.org.'''
        )}]

        changes = []
        slicing = None
        seen = set()
        default_printer_id = None
        default_extruder_id = None
        has_models = True
        errors = 0
        api_count = 0
        self._transport_attempts = 0
        api_seconds = 0.0
        nudged_for_action = False
        assistant_notes = []
        last_stage = "Начальное чтение контекста слайсера"
        started_at = time.monotonic()

        try:
            self._check(cancel, deadline)
            initial_loaded = False
            try:
                initial = self.tools.on_main_thread(
                    self.tools.initial_context, cancel_event=cancel, deadline=deadline
                )
                ctx = initial.get('context', {}) if isinstance(initial, dict) else {}
                printer_obj = ctx.get('printer')
                extruder_obj = ctx.get('active_extruder')

                default_printer_id = (
                    printer_obj.get('id') if isinstance(printer_obj, dict)
                    else ctx.get('printer_id')
                )
                default_extruder_id = (
                    extruder_obj.get('id') if isinstance(extruder_obj, dict)
                    else ctx.get('extruder_id')
                )
                has_models = bool(ctx.get('models'))
                initial_loaded = True

                messages.append({
                    'role': 'system',
                    'content': (
                        'КОНТЕКСТ СЛАЙСЕРА (принтер, модели, доступные материалы и текущие настройки): '
                        + json.dumps(initial, ensure_ascii=False, allow_nan=False)
                    )
                })
            except Exception as error:
                self._check(cancel, deadline)
                if print_settings is not None:
                    messages.append({'role': 'system', 'content': 'Снимок настроек: ' + json.dumps(print_settings, ensure_ascii=False)})
                messages.append({
                    'role': 'system',
                    'content': 'Начальный контекст недоступен: ' + str(error).replace(config['key'], '[hidden]')
                })

            if not initial_loaded and print_settings is not None:
                messages.append({'role': 'system', 'content': 'Снимок настроек: ' + json.dumps(print_settings, ensure_ascii=False)})
            if task_text:
                messages.append({'role': 'user', 'content': 'Техническое задание (данные документа):\n' + task_text})
            messages.extend(history[-4:])
            messages.append({'role': 'user', 'content': text})

            intent = self._classify_intent(text, task_text=task_text, has_models=has_models)
            if re.fullmatch(r'(да|давай|продолжить|продолжай|начинай|делай|ок)[.! ]*', text.strip().lower()):
                previous = next((h.get('content','') for h in reversed(history) if h.get('role') == 'user'), '')
                intent = self._classify_intent(previous, has_models=has_models)
            is_full_slice_task = (intent == 'full_slice_task')
            include_diagnostics = True
            messages.append({'role': 'system', 'content': 'Режим разрешённых действий: ' + intent + '. question/diagnostic: только чтение; settings_only: без нарезки.'})
            pipeline_failed = False
            pending_failures = set()

            for step in range(self.MAX_STEPS):
                notify({'phase': 'status', 'message': 'Запрос к LLM: шаг {0}/{1}'.format(step + 1, self.MAX_STEPS)})
                remaining = deadline - time.monotonic()
                is_last_step = (step == self.MAX_STEPS - 1) or (remaining < 15)
                slice_done = slicing is not None and slicing.get('status') == 'completed'

                if slice_done:
                    return self._summary(changes, slicing)

                final_only = slice_done or (is_last_step and slicing is not None)

                if final_only:
                    messages.append({
                        'role': 'user',
                        'content': '[FINAL_RESPONSE] Кратко опиши фактический результат инструментов. Не утверждай завершение при ошибке.'
                    })
                elif is_last_step and is_full_slice_task and slicing is None:
                    messages.append({
                        'role': 'user',
                        'content': 'Заверши разрешённую подготовку инструментами. Если есть неустранимая ошибка, сообщи её; не запускай нарезку после ошибок.'
                    })

                last_stage = "Ожидание ответа API, обращение {0}".format(api_count + 1)
                started = time.monotonic()
                api_count += 1
                try:
                    message = self._bounded_request(
                        config, messages, cancel, deadline, include_diagnostics=include_diagnostics, notify=notify
                    )
                except Exception:
                    if slice_done:
                        return self._summary(changes, slicing)
                    raise
                finally:
                    api_seconds += time.monotonic() - started
                notify({'phase': 'status', 'message': 'Ответ API за {0:.1f} с'.format(time.monotonic() - started)})

                raw_content = message.get('content') if isinstance(message.get('content'), str) else ''
                calls = message.get('tool_calls') or []

                if not calls and not final_only and raw_content:
                    parsed_calls = self._extract_text_tool_calls(raw_content)
                    if parsed_calls:
                        calls = parsed_calls

                cleaned_content = self._clean_text(raw_content)
                if cleaned_content and not any(k in cleaned_content for k in ('set_material(', 'set_print_settings(', 'start_slicing(')):
                    assistant_notes.append(cleaned_content)

                if final_only and calls:
                    calls = []

                if not calls:
                    reply = cleaned_content or raw_content.strip()
                    if not reply:
                        if slice_done:
                            return self._summary(changes, slicing)
                        raise ValueError('API вернул пустой ответ.')

                    # Если настройки уже были изменены в рамках полной задачи на подготовку, а нарезка ещё не запущена — запускаем автоматически!
                    if is_full_slice_task and changes and slicing is None and has_models and not pipeline_failed:
                        try:
                            last_stage = 'Автозапуск нарезки после применения настроек'
                            slicing, _ = self._run_slicing_and_wait(cancel, deadline, notify)
                        except Exception as slice_err:
                            slicing = {'status': 'error', 'error': str(slice_err)}
                        return self._summary(changes, slicing) + '\n\n' + reply

                    # Если это задача на подготовку/нарезку, а модель вообще не вызвала инструменты — напоминаем 1 раз:
                    if is_full_slice_task and not changes and slicing is None and not nudged_for_action and not is_last_step:
                        nudged_for_action = True
                        messages.append({'role': 'assistant', 'content': raw_content})
                        messages.append({
                            'role': 'user',
                            'content': 'Подготовка уже разрешена. Выполни необходимые инструменты без повторного подтверждения; при неустранимой ошибке сообщи причину.'
                        })
                        continue

                    if changes or slicing or pipeline_failed:
                        reason = 'Подготовка не завершена: ' + ', '.join(sorted(pending_failures)) if pipeline_failed else None
                        return self._summary(changes, slicing, reason)
                    return reply

                if not isinstance(calls, list) or len(calls) > 8:
                    raise ValueError('Слишком много вызовов инструментов за один шаг.')
                ids = set()
                for call in calls:
                    if (not isinstance(call, dict) or not isinstance(call.get('id'), str)
                            or call['id'] in ids or call.get('type') != 'function'):
                        raise ValueError('Некорректный вызов инструмента от API.')
                    ids.add(call['id'])

                # Выполняем вызовы строго в технологическом порядке: 1) set_material -> 2) set_print_settings -> 3) start_slicing
                priority = {'load_model': -1, 'orient_model': 1, 'set_material': 2, 'set_print_settings': 3, 'arrange_models': 4, 'validate_geometry': 5, 'start_slicing': 6}
                ordered_calls = sorted(
                    calls,
                    key=lambda c: priority.get(c.get('function', {}).get('name', ''), 0)
                )

                messages.append({'role': 'assistant', 'content': message.get('content'), 'tool_calls': calls})

                called_names_in_step = {c.get('function', {}).get('name', '') for c in ordered_calls}
                settings_applied_in_step = False
                blocked_batch = False

                for call in ordered_calls:
                    self._check(cancel, deadline)
                    function = call.get('function', {})
                    name = function.get('name', '')
                    try:
                        mutating = name in ('set_material', 'set_print_settings', 'start_slicing', 'load_model', 'orient_model', 'arrange_models')
                        if mutating and (intent in ('question', 'diagnostic') or (name == 'start_slicing' and intent != 'full_slice_task')):
                            raise ValueError('Этот запрос разрешает только чтение или изменение без нарезки.')
                        if mutating and blocked_batch:
                            raise ValueError('Предыдущая операция пакета не выполнена; исправьте её перед продолжением.')
                        if name == 'start_slicing' and pipeline_failed:
                            raise ValueError('Осталась ошибка подготовки. Исправьте материал/настройки перед нарезкой.')
                        raw = function.get('arguments', '{}')
                        if not isinstance(raw, str) or len(raw) > 20000:
                            raise ValueError('Слишком большие или неверные аргументы.')
                        arguments = json.loads(raw) if raw.strip() else {}
                        if not isinstance(arguments, dict):
                            raise ValueError('Аргументы инструмента должны быть JSON-объектом.')

                        if name == 'set_material':
                            if not arguments.get('printer_id') and default_printer_id:
                                arguments['printer_id'] = default_printer_id
                            if not arguments.get('extruder_id') and default_extruder_id:
                                arguments['extruder_id'] = default_extruder_id

                        if name == 'set_print_settings':
                            if 'values' not in arguments or not isinstance(arguments.get('values'), dict):
                                raise ValueError('Передайте словарь настоящих ключей слайсера в поле "values".')

                        fingerprint = name + json.dumps(arguments, sort_keys=True, ensure_ascii=False)
                        if fingerprint in seen and name not in ('get_scene_models', 'analyse_orientation', 'validate_geometry'):
                            result = {'ok': True, 'result': {'note': 'Вызов уже выполнен ранее в этом запросе.'}}
                            messages.append({
                                'role': 'tool',
                                'tool_call_id': call['id'],
                                'content': json.dumps(result, ensure_ascii=False, allow_nan=False)
                            })
                            continue

                        if name == 'start_slicing':
                            last_stage = 'Инструмент: start_slicing'
                            slicing, result = self._run_slicing_and_wait(cancel, deadline, notify)
                            seen.add(fingerprint)
                            messages.append({
                                'role': 'tool',
                                'tool_call_id': call['id'],
                                'content': json.dumps(result, ensure_ascii=False, allow_nan=False)
                            })
                            continue

                        notify({'phase': 'start', 'name': name, 'arguments': arguments})
                        last_stage = 'Инструмент: ' + name
                        output = self.tools.call(name, arguments, cancel_event=cancel, deadline=deadline)
                        seen.add(fingerprint)

                        if name in ('load_model','orient_model','arrange_models'):
                            changes.extend(output.get('changes', []))
                            pending_failures.discard(name)
                            pipeline_failed = bool(pending_failures)
                            seen.clear()
                            has_models = True
                        if name == 'list_materials':
                            default_printer_id = output.get('printer_id') or default_printer_id
                            default_extruder_id = output.get('extruder_id') or default_extruder_id
                        elif name == 'set_material':
                            changes.extend(output.get('changes', []))
                            if output.get('requires_user_action'):
                                notify({'phase':'result','name':name,'output':{'ok':True,'result':output}})
                                return self._summary(changes, slicing, '\n'.join(output.get('warnings', [])))
                            pending_failures.discard(name)
                            pipeline_failed = bool(pending_failures)
                            seen = {
                                item for item in seen
                                if not item.startswith((
                                    'get_print_settings', 'list_print_settings',
                                    'get_print_context', 'set_print_settings', 'start_slicing'
                                ))
                            }
                        elif name == 'set_print_settings':
                            changes.extend(output.get('changes', []))
                            if output.get('skipped'):
                                raise ValueError('Есть неприменённые настройки: ' + json.dumps(output['skipped'],ensure_ascii=False))
                            pending_failures.discard(name)
                            pipeline_failed = bool(pending_failures)
                            settings_applied_in_step = True
                            seen = {
                                item for item in seen
                                if not item.startswith(('get_print_settings', 'start_slicing'))
                            }

                        result = {'ok': True, 'result': output}
                    except Exception as error:
                        errors += 1
                        if name in ('set_material', 'set_print_settings', 'start_slicing', 'load_model', 'orient_model', 'arrange_models'):
                            if not blocked_batch:
                                pending_failures.add(name)
                            blocked_batch = True
                            pipeline_failed = True
                        result = {'ok': False, 'error': str(error).replace(config['key'], '[hidden]')}
                    notify({'phase': 'result', 'name': name, 'output': result})
                    if errors >= 5:
                        raise RuntimeError('Ошибки инструментов: ' + result.get('error', 'проверьте параметры'))
                    messages.append({
                        'role': 'tool',
                        'tool_call_id': call['id'],
                        'content': json.dumps(result, ensure_ascii=False, allow_nan=False)
                    })

                if slicing is not None and slicing.get('status') == 'completed':
                    return self._summary(changes, slicing)

            if is_full_slice_task and changes and slicing is None and has_models and not pipeline_failed:
                try:
                    slicing, _ = self._run_slicing_and_wait(cancel, deadline, notify)
                except Exception as slice_err:
                    slicing = {'status': 'error', 'error': str(slice_err)}

            if slicing and slicing.get('status') == 'completed':
                if assistant_notes:
                    return self._summary(changes, slicing)
                return self._summary(changes, slicing)

            if changes:
                if assistant_notes:
                    return self._summary(changes, slicing)
                return self._summary(changes, slicing)

            raise RuntimeError('Достигнут предел {0} шагов. Задача не завершена.'.format(self.MAX_STEPS))
        except Exception as error:
            if slicing and slicing.get('status') in ('starting', 'processing', 'done_signal'):
                try:
                    slicing = self.tools.cancel_slicing()
                except Exception:
                    slicing = {'status': 'не подтверждён', 'error': 'Не удалось остановить нарезку; проверьте слайсер.'}
            diagnostic = (
                "\nДиагностика: {0} обращений к API; ожидание API — {1:.1f} с; "
                "всего — {2:.1f} с. Последний этап: {3}."
            ).format(api_count, api_seconds, time.monotonic() - started_at, last_stage) + '\nСетевых попыток с учётом повторов: {0}.'.format(self._transport_attempts)
            return self._summary(changes, slicing, str(error).replace(config['key'], '[hidden]') + diagnostic)
