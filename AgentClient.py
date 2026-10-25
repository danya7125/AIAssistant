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
from urllib.parse import urlsplit
from .WebServer import setting_schema, dispatch_setting
from urllib.error import HTTPError, URLError
from .DomainKnowledge import DomainKnowledge


class AgentClient:
    RETRY_DELAYS = (2, 5)
    MAX_STEPS = 12
    TASK_TIMEOUT = 1800
    REQUEST_TIMEOUT = 300

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
            payload['tools'] = [t for t in payload['tools'] if t.get('function', {}).get('name') != 'set_setting'] + [setting_schema()]
            payload['tool_choice'] = 'auto'

        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
        endpoint = urlsplit(config['url'].rstrip('/') + '/chat/completions')
        if endpoint.scheme != 'https' or not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
            raise ValueError('Нужен HTTPS URL API без логина, query и fragment.')
        headers = {
            'Authorization': 'Bearer ' + config['key'],
            'Content-Type': 'application/json',
            'Connection': 'keep-alive'
        }
        context = ssl.create_default_context()
        ca_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cacert.pem')
        if os.path.isfile(ca_file):
            context.load_verify_locations(cafile=ca_file)
        try:
            # urllib.request forces Connection: close internally. HTTPSConnection
            # preserves our header; each attempt owns and closes its own socket.
            connection = http.client.HTTPSConnection(endpoint.hostname, endpoint.port or 443,
                timeout=min(self.REQUEST_TIMEOUT, timeout), context=context)
            try:
                connection.request('POST', endpoint.path or '/', body=body, headers=headers)
                response = connection.getresponse()
                raw = response.read(1024 * 1024 + 1)
                if response.status >= 400:
                    raise HTTPError(config['url'], response.status, response.reason, response.headers, None)
                if response.status != 200:
                    raise RuntimeError('API вернул неожиданный HTTP-статус {0}. Перенаправления не выполняются.'.format(response.status))
            finally:
                connection.close()
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
                if not isinstance(candidates, list) or not candidates:
                    continue

                # 1. Всегда находим текущее положение детали (0 градусов)
                current = None
                for c in candidates:
                    if c.get('degrees') == 0:
                        current = c
                        break
                if current is None and candidates:
                    current = candidates[0]

                feature_det = report.get('feature_detection', {})
                is_litho = bool(feature_det.get('is_thin_relief_or_lithophane') or feature_det.get('is_image_file'))
                is_flat = bool(feature_det.get('is_lying_flat'))

                # 2. Определение деталей, стоящих на тонком ребре / ободе (колеса, крыльчатки, диски, шестерни)
                curr_contact = float(current.get('planar_contact_mm2') or 0) if current else 0.0
                max_alt_contact = max([float(c.get('planar_contact_mm2') or 0) for c in candidates] or [0.0])
                is_standing_on_edge = bool(max_alt_contact > 200.0 and curr_contact < 0.20 * max_alt_contact and curr_contact < 350.0)

                if is_standing_on_edge:
                    best_flat_cand = max(candidates, key=lambda c: float(c.get('planar_contact_mm2') or 0))
                    best_flat_cand['edge_standing_fix_recommended'] = True

                # Выбираем до 3 лучших альтернативных кандидатов
                pareto_alts = [
                    c for c in candidates
                    if (c.get('pareto_candidate') or c.get('lithophane_recommended') or c.get('edge_standing_fix_recommended')) and c.get('degrees') != 0
                ]
                if is_litho and is_flat:
                    pareto_alts.sort(key=lambda c: (
                        not c.get('lithophane_recommended'),
                        -(c.get('planar_contact_mm2') or 0),
                        c.get('height_mm') or 9999
                    ))
                elif is_standing_on_edge:
                    pareto_alts.sort(key=lambda c: (
                        not c.get('edge_standing_fix_recommended'),
                        -(c.get('planar_contact_mm2') or 0),
                        c.get('height_mm') or 9999
                    ))
                else:
                    pareto_alts.sort(key=lambda c: (
                        -(c.get('planar_contact_mm2') or 0),
                        c.get('height_mm') or 9999
                    ))
                top_alts = pareto_alts[:3]

                def _trim_cand(c, is_curr=False):
                    cd = {
                        'axis': c.get('axis', 'x'),
                        'degrees': c.get('degrees', 0),
                        'height_mm': round(float(c.get('height_mm') or 0), 1),
                        'contact_area_mm2': round(float(c.get('planar_contact_mm2') or 0), 1),
                        'support_proxy_mm3': round(float(c.get('support_to_plate_prism_sum_mm3') or 0), 1),
                        'is_current': is_curr
                    }
                    if c.get('lithophane_recommended'):
                        cd['lithophane_recommended'] = True
                    if c.get('edge_standing_fix_recommended'):
                        cd['edge_standing_fix_recommended'] = True
                    return cd

                compact_candidates = []
                if current is not None:
                    compact_candidates.append(_trim_cand(current, is_curr=True))
                for c in top_alts:
                    compact_candidates.append(_trim_cand(c, is_curr=False))

                mode_in = report.get('inputs', {}).get('printing_mode') or ''
                if is_standing_on_edge:
                    note_text = (
                        'КРИТИЧЕСКОЕ ПРЕДУПРЕЖДЕНИЕ: Деталь импортирована СТОЯ НА ТОНКОМ ОБОДЕ/РЕБРЕ! '
                        'Площадь контакта при 0° ничтожно мала ({0} мм² против {1} мм² при укладке плашмя). '
                        'Деталь оторвется от стола при печати, а при вращении 2900 об/мин её разорвет по слоям Z! '
                        'СТРОГО НЕОБХОДИМ ПОВОРОТ НА 90° (orient_model по кандидату с edge_standing_fix_recommended=True), '
                        'чтобы деталь легла плашмя на торец ступицы, а слои сформировали прочные кольца в плоскости XY!'
                    ).format(round(curr_contact, 1), round(max_alt_contact, 1))
                elif is_litho and is_flat:
                    note_text = 'ВНИМАНИЕ: Геометрический сканер определил деталь как КАРТИНУ / ЛИТОФАНИЮ (рельефная пластина из JPG/PNG). КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО использовать эластомеры (TPU) или 0% заполнения! ЗОЛОТОЙ ПРОФИЛЬ ДЛЯ КАРТИН: 1) Режим: classic (3D на плоском столе). 2) Ориентация: поворот на 90° вертикально на ребро (кандидат с lithophane_recommended=True) для передачи полутонов тонкими слоями 0.12 мм. 3) Материал: Proto PLA (Белый). 4) Заполнение: 100% (монолит без пустот во избежание просвечивания сетки). 5) Стенки: 8. 6) Кайма: Brim 12 мм (обязательна для удержания тонкой вертикальной пластины). 7) Поддержки: False, обдув: 100%.'
                elif mode_in in ('cylindrical', 'cylindrical_full'):
                    note_text = 'ВНИМАНИЕ: Активен режим 5D-печати Stereotech (на вращающейся оправке). Если деталь соосна оправке, поворот не требуется (Закон 0°). Режим 5D сохраняется для нарезки.'
                else:
                    note_text = 'Показано текущее положение (0°) и лучшие альтернативы по устойчивости. Если текущее положение обеспечивает достаточную площадь контакта и правильную укладку слоев вдоль изгибающих нагрузок, поворот не требуется (Закон 0°).'

                compact_report = {
                    'model_id': report.get('model_id'),
                    'detected_feature': 'lithophane_or_thin_relief' if (is_litho and is_flat) else 'standard_mesh',
                    'current_orientation': compact_candidates[0] if compact_candidates else None,
                    'top_candidates': compact_candidates,
                    'note': note_text
                }
                obj['result'] = compact_report
                msg['content'] = json.dumps(obj, ensure_ascii=False, allow_nan=False)
            except (ValueError, TypeError, AttributeError):
                continue
        return result

    @staticmethod
    def _retryable(error):
        seen = set()
        current = error
        temporary = False
        while isinstance(current, BaseException) and id(current) not in seen:
            seen.add(id(current))
            if isinstance(current, ssl.SSLEOFError) or (
                    isinstance(current, ssl.SSLError) and getattr(current, 'reason', '') in
                    ('UNEXPECTED_EOF_WHILE_READING', 'EOF_OCCURRED_IN_VIOLATION_OF_PROTOCOL')):
                temporary = True
            elif isinstance(current, ssl.SSLError):
                return False  # Never retry invalid certificates or disable verification.
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

    def _profile_card(self, text):
        # Values come from STE Slicer, never from a guessed LLM report.
        from .ProfileCard import card_from_snapshot
        snapshot = self.tools.on_main_thread(self.tools.profile_snapshot)
        return json.dumps(card_from_snapshot(snapshot, text), ensure_ascii=False)

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
        text = '\n'.join(lines)
        if not reason and (changes or slicing) and (not slicing or slicing.get('status') == 'completed'):
            try:
                return self._profile_card(text)
            except Exception:
                # A missing snapshot must not hide the outcome of the operation.
                pass
        return text

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
        t = re.sub(r'аппаратные параметры не меняй[.!;]?', '', t)
        
        # 1. Жесткая защита от случайных срабатываний на приветствия
        if re.search(r'^(привет|здравствуй|добрый\s*(день|вечер|утро)|hello|hi)', t):
            return 'question'
            
        # 2. Режим только чтения
        readonly = re.search(r'не\s+(?:меня\w*|изменя\w*|применя\w*|трогай)|ничего\s+не|только\s+(?:оцени\w*|покажи\w*|объясни\w*|сравни\w*)', t)
        diagnostic = re.search(r'провер\w*\s+(?:геометр\w*|сетк\w*|модел\w*|g-?code|траектор\w*)', t)
        
        if readonly:
            return 'diagnostic' if diagnostic else 'question'
        if diagnostic:
            return 'diagnostic'
            
        no_slice = re.search(r'без\s+нарез\w*|не\s+(?:нарез\w*|режь|запуска\w*\s+нарез\w*)', t)
        
        if task_text or re.search(r'\bтз\b|техническ\w*\s+задан\w*|условия\s+эксплуатации', t):
            return 'settings_only' if no_slice else 'full_slice_task'
            
        # 3. Полный цикл (настройки + нарезка)
        full = re.search(r'нарежь|нарезать|подготов\w*|настрой(?:те)?.*(?:модель|деталь|печать)|запусти\w*.*нарез', t)
        if full and not no_slice:
            return 'full_slice_task'
            
        # 4. Обычные вопросы
        is_question = re.match(r'^(?:покажи|оцени|сравни|расскажи|объясни|почему|зачем|что|какой|какие|как)\b', t)
        if is_question:
            return 'question'
            
        # 5. Изменение настроек БЕЗ нарезки
        action = re.search(r'\b(?:загрузи\w*|открой|положи\w*|поверни\w*|размести\w*|расставь\w*|ориентируй\w*|примени\w*|выстави\w*|установи\w*|поставь\w*|поменяй\w*|смени\w*|измени\w*|включи\w*|выключи\w*|подбери\w*|выбери\w*)\b', t)
        if action:
            return 'settings_only'
            
        # По умолчанию - абсолютно безопасный режим диалога
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

        compact_materials = DomainKnowledge.get_compact_summary()
        messages = [{'role': 'system', 'content': (
            '''Ты ведущий инженер-технолог аддитивного производства и эксперт STE Slicer.
Твоя цель — безупречная инженерная подготовка 3D-моделей к печати: правильная ориентация с учетом анизотропии FDM, безошибочный подбор материала Stereotech и технологических параметров.

=== ПРИОРИТЕТ 0: КАРТИНЫ, ЛИТОФАНИИ И ФОТО-БАРЕЛЬЕФЫ (ИЗ JPG/PNG ИЛИ ТОНКИЕ РЕЛЬЕФНЫЕ ПЛАСТИНЫ) ===
ФИЗИЧЕСКАЯ ГЕОМЕТРИЯ ПЛАСТИНЫ ИМЕЕТ АБСОЛЮТНЫЙ ПРИОРИТЕТ НАД НАЗВАНИЕМ ДЕТАЛИ!
Если в get_scene_models имя файла заканчивается на графическое расширение (.jpg, .jpeg, .png, .bmp, .webp, .tif)
ЛИБО если analyse_orientation вернул detected_feature = "lithophane_or_thin_relief":
Перед тобой КАРТИНА (литофания), а НЕ техническая деталь! Даже если файл назван «пыльник», «вал», «колесо», «шестерня» — это барельеф из картинки, к которому КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО применять TPU (эластомеры), композиты или 0% заполнения!

ЗОЛОТОЙ ИНЖЕНЕРНЫЙ СТАНДАРТ ДЛЯ ЛЮБОЙ КАРТИНЫ:
1. РЕЖИМ ПЕЧАТИ: УВАЖАЙ ВЫБОР ПОЛЬЗОВАТЕЛЯ! Если пользователь в слайсере выбрал 5D — сохраняй 5D (cylindrical/cylindrical_full), НЕ ПЕРЕКЛЮЧАЙ в classic! Если выбран classic — печатай на плоском столе в 3D.
2. МАТЕРИАЛ: set_material("proto_pla", ...). Исключительно белый жесткий Proto PLA. Он обладает идеальной оптической полупрозрачностью для проявления полутонов на просвет. Эластомеры и черные/темные пластики разрушают оптический эффект!
3. ОРИЕНТАЦИЯ: orient_model с поворотом на 90° вертикально на ребро (выбрать кандидат с lithophane_recommended=True).
   - ПОЧЕМУ: При печати плашмя высота пластины всего 2-4 мм (это 15-30 слоев) — фото превратится в грубые ступени с потерей полутонов. При повороте на 90° вертикально ось Z делает тысячи микрошагов по 0.12 мм, передавая фотографическую детализацию!
4. ПАРАМЕТРЫ ПЕЧАТИ (set_print_settings):
   - infill_sparse_density: 100.0 (СТРОГО монолит без внутренних пустот и решеток, иначе подсветка покажет ребра заполнения!).
   - wall_line_count: 8 (монолитная укладка контура).
   - layer_height: 0.12 (первый слой layer_height_0: 0.20) для тончайшего разрешения.
   - adhesion_type: "brim", brim_width: 12.0 (широкая кайма обязательна: узкая высокая пластина без каймы упадет при печати!).
   - support_enable: False (поддержки не требуются и испортят барельеф).
   - cool_fan_enabled: True, cool_fan_speed: 100.0.
   - speed_print: 30.0 (умеренная скорость для жесткости высокой детали).
   - outer_inset_first: True.

=== ПРАВИЛА 5D-ПЕЧАТИ (STEREOTECH 5D HYBRID / CYLINDRICAL) ===
Если в контексте слайсера printing_mode равен "cylindrical" или "cylindrical_full" (или пользователь выбрал 5D):
1. СОХРАНЕНИЕ 5D: СТРОЖАЙШЕ ЗАПРЕЩЕНО менять или сбрасывать printing_mode на "classic"! Режим 5D ОБЯЗАН оставаться активным!
2. ОРИЕНТАЦИЯ В 5D: Если деталь уже соосна оправке — поворот не требуется (0°). НО ЕСЛИ круглая деталь (колесо, шкив, шестерня) импортирована стоя на ребре/ободе, её ОБЯЗАТЕЛЬНО нужно повернуть на 90° (orient_model по кандидату с edge_standing_fix_recommended=True), чтобы положить плашмя на торец ступицы!
3. ПАРАМЕТРЫ 5D (set_print_settings):
   - cylindrical_mode_base_diameter: диаметр оправки станка (например 6, 8, 10, 12, 16 мм). Всегда должен быть > 0.
   - infill_sparse_density: 70..100% (для прочных втулок, валов и непрерывной укладки слоев вокруг оси).
   - wall_line_count: 4..8 (прочный силовой контур вокруг оправки).
   - adhesion_type: "skirt" (в 5D кайма brim на стол не применяется).
4. МАТЕРИАЛЫ 5D: Конструкционные полимеры Stereotech под нагрузки вращения и скручивания: Fiberpart PC G20 (жесткость и термостойкость), Ultrax / PA6-CF (сверхпрочность с углеволокном), Enduse PA6 (для трения скольжения).

=== ТРИ ФИЗИЧЕСКИХ ЗАКОНА FDM И ОРИЕНТАЦИИ ===
1. ЗАКОН АНИЗОТРОПИИ FDM:
   Межслойная прочность (по оси Z) составляет лишь 40-60% от прочности монолитной нити в плоскости XY. При изгибе и растяжении деталь ВСЕГДА разрушается по границе слоев. Поэтому растягивающие и изгибающие напряжения ОБЯЗАНЫ лежать строго в плоскости XY!
2. ЗАКОН НУЛЕВОГО ПОВОРОТА И ОБЯЗАТЕЛЬНЫЕ ИСКЛЮЧЕНИЯ:
   Всегда оценивай исходную ориентацию и площадь контакта при 0°:
   - ЕСЛИ модель УЖЕ лежит на широкой плоскости, вал УЖЕ лежит горизонтально, а шестерня УЖЕ лежит плашмя — поворот НЕ планируй (0°).
   - КРИТИЧЕСКОЕ ИСКЛЮЧЕНИЕ (Колеса, крыльчатки, диски, шестерни, шкивы):
     Если круглая деталь вращения импортирована СТОЯ НА ОБОДЕ / РЕБРЕ (как колесо велосипеда на дороге) — ВЫЗОВ orient_model НА 90° ПЛАШМЯ НА ТОРЕЦ СТУПИЦЫ СТРОЖАЙШЕ ОБЯЗАТЕЛЕН!
     ФИЗИЧЕСКОЕ ОБОСНОВАНИЕ:
     1) Площадь опоры на ободе равна ~0 — деталь не удержится на столе и упадет при печати.
     2) Центробежные силы на 2900 об/мин и крутящий момент шпонки действуют радиально: колесо расколется поперек слоев Z.
     3) При повороте на 90° плашмя на торец ступицы слои укладываются сплошными неразрывными кольцами в плоскости XY — максимальная прочность на разрыв и срез!
3. ЗАКОН ТРИБОЛОГИЧЕСКОЙ СОВМЕСТИМОСТИ (ТРЕНИЕ СКОЛЬЖЕНИЯ):
   Композиты с рубленым волокном (Fiberpart GF/CF) на микроуровне работают как наждачная бумага! Их КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО ставить в пары сухого трения с мягкими полимерами (POM/полиацеталь, фторопласт) или цветными металлами (бронза, латунь, алюминий) — волокно сточит ответную деталь за смену. В пары скольжения ставить ИСКЛЮЧИТЕЛЬНО ненаполненный антифрикционный нейлон Enduse PA6!

=== СПРАВОЧНИК МАТЕРИАЛОВ STEREOTECH ===
''' + compact_materials + '''

=== ПРАВИЛА ТЕХНОЛОГИЧЕСКИХ ПАРАМЕТРОВ (set_print_settings) ===
- Силовые шестерни, кулачки, кронштейны: wall_line_count=5..8, infill_sparse_density=70..100%, infill_pattern="gyroid", outer_inset_first=True.
- Эластичные гофры, пыльники, сильфоны (Sealant): wall_line_count=2..3 (толщина стенки 0.8..1.2 мм), infill_sparse_density=0% (иначе деталь потеряет гибкость!).
- Чистовые декоративные фигурки: layer_height=0.10..0.12, speed_print=30..45, outer_inset_first=True.
- Высокотемпературные полимеры и PC (Fiberpart PC G20): cool_fan_speed=0 (без обдува во избежание расслоения слоев), material_print_temperature=290, material_bed_temperature=115.

=== ОБЯЗАТЕЛЬНЫЙ РЕГЛАМЕНТ РАБОТЫ ПО ТЗ (СТРОГО 2 ЭТАПА) ===
ЭТАП 1 (Инженерный анализ и Согласование):
1. Вызови get_scene_models и analyse_orientation.
2. Оцени исходную ориентацию по Закону Нулевого Поворота. Если деталь уже лежит оптимально — поворот НЕ планируй!
3. Подбери точный материал Stereotech по физико-химическим условиям ТЗ.
4. СТОП! НА ЭТАПЕ 1 КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО ВЫЗЫВАТЬ set_material, set_print_settings и start_slicing!
5. Напиши пользователю подробный инженерный вердикт:
   • Обоснование ориентации (почему именно такое положение гарантирует прочность слоев на изгиб/срез).
   • Выбранный материал Stereotech и ключевые технологические параметры (стенки, заполнение, температуры).
6. В конце прямо спроси: "Применяем эти настройки для нарезки?".

ЭТАП 2 (Применение и Нарезка):
1. Жди ответа пользователя.
2. При подтверждении ("Да", "Нарезай", "Ок", "Согласен", "Применяй" и т.п.) последовательно вызови:
   - set_material(material_id="...")
   - orient_model(...) ТОЛЬКО ЕСЛИ поворот действительно был необходим (если ориентация уже оптимальна, не вызывай!)
   - set_print_settings(values={...})
3. Запусти нарезку: start_slicing().

=== ЭТАЛОННЫЕ ПРИМЕРЫ РЕШЕНИЯ ЗАДАЧ (FEW-SHOT GOLDEN CASES) ===
Кейс 0 (Картина / Литофания из JPG/PNG):
• ТЗ: Любая картина / фотография / литофания, например "Портрет.jpg", "Пыльник шруса.jpg" или "Ось вращения.jpg".
• Анализ: Геометрия — тонкая пластина с барельефом. Печать плашмя дает грубую ступеньку. Необходим вертикальный поворот на 90° на ребро.
• Материал: Исключительно Proto PLA (Белый) для идеального светопропускания.
• Параметры: wall_line_count=8, infill_sparse_density=100%, layer_height=0.12, adhesion_type="brim", brim_width=12.0, cool_fan_speed=100, support_enable=False, speed_print=30.0.
• Действие на Этапе 1: Описать преимущества вертикальной печати (фотографическая детализация Z-слоями), предложить Proto PLA White и 100% монолит.

Кейс 1 (Шестерня привода):
• ТЗ: Нагруженная шестерня редуктора, момент 45 Н·м, влажность 80%.
• Анализ: Зубья работают на циклический срез в плоскости вращения. Шестерня обязана лежать плашмя на торце (XY), чтобы контур зуба формировался сплошными периметрами. Исходное положение уже плашмя торцом на столе -> orient_model НЕ вызывать (0°)!
• Материал: Высокий крутящий момент и влажность -> Fiberpart PC G20 (диэлектрик, Tg=145°C, 95 МПа).
• Параметры: wall_line_count=6, infill_sparse_density=100%, gyroid, print_temp=290, bed_temp=115, fan=0.
• Действие на Этапе 1: Выдать отчет и спросить подтверждение.

Кейс 2 (Копирный вал в паре с POM):
• ТЗ: Кулачковый вал, по которому скользят ролики из полиацеталя (POM), абразивная целлюлозная пыль.
• Анализ: Вал работает на изгиб -> должен лежать горизонтально вдоль стола (XY). Исходное положение горизонтально -> orient_model НЕ вызывать (0°)! Пара трения с мягким POM -> композиты GF/CF запрещены (сточат ролики как наждак) -> материал строго Enduse PA6 (антифрикционный ненаполненный нейлон).
• Параметры: wall_line_count=6, infill_sparse_density=80%, gyroid.
• Действие на Этапе 1: Выдать отчет и спросить подтверждение.

Кейс 3 (Эластичный пыльник ШРУС):
• ТЗ: Защитный гофрированный чехол ШРУС, смазка MoS2, изгиб до 42°, дорожные реагенты.
• Анализ: Сильфон должен сжиматься и изгибаться как пружина -> ставить вертикально на фланец. Исходное положение вертикально -> orient_model НЕ вызывать (0°)! Требуется эластичность >300% и стойкость к MoS2 -> Sealant TPU 93.
• Параметры: wall_line_count=2, infill_sparse_density=0% (zero infill для сохранения гибкости гофры!).
• Действие на Этапе 1: Выдать отчет и спросить подтверждение.

Кейс 4 (Ось вращения / Вал / Втулка в режиме 5D):
• ТЗ: Ось вращения передающих звёзд, силовой вал или втулка на оправке 5D (диаметр основания 6 мм).
• Анализ: Режим 5D (cylindrical_full). Деталь базируется на вращающейся оправке станка. Смещение на плоский стол запрещено -> orient_model НЕ вызывать (0°)! Режим 5D сохраняется без изменений!
• Материал: Высокие динамические нагрузки кручения -> Fiberpart PC G20 или Ultrax (PA6-CF).
• Параметры: printing_mode="cylindrical_full", cylindrical_mode_base_diameter=6, wall_line_count=6, infill_sparse_density=100%, adhesion_type="skirt", speed_print=30.0.
• Действие на Этапе 1: Описать преимущества непрерывной 5D-укладки вокруг оправки, подтвердить параметры и запустить нарезку в 5D.

Кейс 5 (Рабочее колесо центробежного насоса / Крыльчатка):
• ТЗ: Рабочее колесо насоса, 2900 об/мин, напор 16 бар, щелочная среда pH 9.5..10.5, гликоль 85°C, шпонка 12 Н·м, запрет углеволокна (гальванопара с алюминием).
• Анализ: Колесо импортировано стоя на круглом ободе -> КАТАСТРОФА! ОБЯЗАТЕЛЕН поворот на 90° плашмя на торец ступицы (orient_model по кандидату с edge_standing_fix_recommended=True)! Слои формируют концентрические кольца в плоскости XY против центробежных сил 2900 об/мин.
• Материал: Fiberpart PP G30 (полипропилен со стекловолокном 30%). Идеальная щелочестойкость pH 1..14, водопоглощение <0.02%, модуль 5500 МПа, стекловолокно — диэлектрик (нет гальванопары в отличие от CF).
• Параметры: wall_line_count=6, infill_sparse_density=100%, gyroid, speed_print=30.0, outer_inset_first=True.
• Действие на Этапе 1: Обосновать поворот на 90° плашмя на торец ступицы, подтвердить Fiberpart PP G30 и 100% заполнение.'''
        )}]
        changes = []
        slicing = None
        seen = set()
        is_picture_scene = False
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
            if re.fullmatch(r'(да|давай|продолжить|продолжай|начинай|делай|ок|применяй|нарезай|согласен|хорошо|печатай|все равно|всё равно)[.! ]*', text.strip().lower()):
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
                        return self._summary(changes, slicing)

                    # Если это задача на подготовку/нарезку, а модель вообще не вызвала инструменты — напоминаем 1 раз:
                    if intent in ('settings_only', 'full_slice_task') and not changes and slicing is None and not nudged_for_action and not is_last_step:
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
                    # LLM may propose a profile, but cannot attest that it was applied.
                    show_model_intent = bool(re.search(
                        r'(?:покажи|выведи|отобрази|открой|визуализируй|дай|где)\s*(?:мне\s*)?(?:3d|3д)?\s*(?:модел\w*|карточк\w*|детал\w*|профил\w*)',
                        (text or '').lower()
                    )) or bool(re.search(r'\b(?:3d|3д)\s*модел\w*', (text or '').lower()))
                    if show_model_intent and has_models:
                        try:
                            return self._profile_card(reply or 'Текущая 3D-модель со стола SteSlicer:')
                        except Exception:
                            pass

                    from .ProfileCard import parse_card
                    card = parse_card(reply)
                    if card is not None:
                        card = dict(card)
                        card['applied'] = False
                        return json.dumps(card, ensure_ascii=False)
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
                priority = {'load_model': -1, 'orient_model': 1, 'set_material': 2, 'set_print_settings': 3, 'set_setting': 3, 'arrange_models': 4, 'validate_geometry': 5, 'start_slicing': 6}
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
                    failure_key = name
                    try:
                        mutating = name in ('set_material', 'set_print_settings', 'set_setting', 'start_slicing', 'load_model', 'orient_model', 'arrange_models')
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

                        if name == 'set_setting':
                            failure_key = 'set_setting:' + str(arguments.get('key', ''))
                        if name == 'set_material':
                            if not arguments.get('printer_id') and default_printer_id:
                                arguments['printer_id'] = default_printer_id
                            if not arguments.get('extruder_id') and default_extruder_id:
                                arguments['extruder_id'] = default_extruder_id
                            if is_picture_scene:
                                mat_req = str(arguments.get('material_id', '')).lower()
                                if any(bad in mat_req for bad in ('tpu', 'sealant', 'flex')):
                                    arguments['material_id'] = 'proto_pla' 

                        if name == 'set_print_settings':
                            if 'values' not in arguments or not isinstance(arguments.get('values'), dict):
                                raise ValueError('Передайте словарь настоящих ключей слайсера в поле "values".')
                            s_vals = arguments['values']
                            # Приоритет выбора пользователя: если в интерфейсе включен 5D, не даем перетереть его в 3D (classic)
                            active_pm = None
                            if isinstance(initial, dict) and isinstance(initial.get('settings'), dict):
                                active_pm = initial['settings'].get('printing_mode')
                            if active_pm in ('cylindrical', 'cylindrical_full'):
                                if s_vals.get('printing_mode') == 'classic':
                                    del s_vals['printing_mode']

                            if is_picture_scene:
                                if active_pm not in ('cylindrical', 'cylindrical_full'):
                                    # В классическом 3D для картин всегда монолит и кайма
                                    if 'infill_sparse_density' in s_vals and float(s_vals['infill_sparse_density']) < 90.0:
                                        s_vals['infill_sparse_density'] = 100.0
                                    if s_vals.get('adhesion_type') in ('none', 'skirt'):
                                        s_vals['adhesion_type'] = 'brim'
                                        s_vals['brim_width'] = 12.0
                                    if 'support_enable' in s_vals:
                                        s_vals['support_enable'] = False
                                    if 'wall_line_count' in s_vals and int(s_vals['wall_line_count']) < 6:
                                        s_vals['wall_line_count'] = 8



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
                        if name == 'set_setting':
                            output = dispatch_setting(self.tools, arguments, cancel_event=cancel, deadline=deadline)
                        else:
                            output = self.tools.call(name, arguments, cancel_event=cancel, deadline=deadline)
                        seen.add(fingerprint)

                        if name == 'get_scene_models' and isinstance(output, dict) and 'models' in output:
                            for sm in output['models']:
                                sm_name = str(sm.get('name') or '').lower()
                                if any(sm_name.endswith(ext) or (ext + '.') in sm_name for ext in ('.jpg', '.jpeg', '.png', '.bmp', '.webp', '.tif', '.tiff')) or any(kw in sm_name for kw in ('литофан', 'lithophane', 'картин', 'барельеф')):
                                    is_picture_scene = True

                        if name == 'analyse_orientation' and isinstance(output, dict):
                            s_feat = output.get('feature_detection', {})
                            if s_feat.get('is_thin_relief_or_lithophane') or s_feat.get('is_image_file'):
                                is_picture_scene = True

                        if name in ('load_model','orient_model','arrange_models'):
                            changes.extend(output.get('changes', []))
                            pending_failures.discard(failure_key)
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
                            pending_failures.discard(failure_key)
                            pipeline_failed = bool(pending_failures)
                            seen = {
                                item for item in seen
                                if not item.startswith((
                                    'get_print_settings', 'list_print_settings',
                                    'get_print_context', 'set_print_settings', 'set_setting', 'start_slicing'
                                ))
                            }
                        elif name in ('set_print_settings', 'set_setting'):
                            changes.extend(output.get('changes', []))
                            if output.get('skipped'):
                                raise ValueError('Есть неприменённые настройки: ' + json.dumps(output['skipped'],ensure_ascii=False))
                            pending_failures.discard(failure_key)
                            pipeline_failed = bool(pending_failures)
                            settings_applied_in_step = True
                            seen = {
                                item for item in seen
                                if not item.startswith(('get_print_settings', 'start_slicing'))
                            }

                        result = {'ok': True, 'result': output}
                    except Exception as error:
                        errors += 1
                        if name in ('set_material', 'set_print_settings', 'set_setting', 'start_slicing', 'load_model', 'orient_model', 'arrange_models'):
                            if not blocked_batch:
                                pending_failures.add(failure_key)
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

            if pipeline_failed:
                return self._summary(changes, slicing, 'Подготовка не завершена: ' + ', '.join(sorted(pending_failures)))

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
