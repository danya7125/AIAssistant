from .SliceEstimates import extract as extract_estimates
import time
from UM.Application import Application
from UM.Backend.Backend import BackendState


class SliceMonitor:
    """Tracks a slice started by this assistant on the Qt main thread."""
    def __init__(self):
        self.backend = None
        self.connections = []
        self.job = None

    def _disconnect(self):
        for signal, callback in self.connections:
            try:
                signal.disconnect(callback)
            except Exception:
                pass
        self.connections = []

    def _connect(self, name, callback):
        signal = getattr(self.backend, name, None)
        if signal is None:
            raise RuntimeError('Движок не предоставляет сигнал ' + name)
        signal.connect(callback)
        self.connections.append((signal, callback))

    def start(self):
        app = Application.getInstance()
        backend = app.getBackend()
        if backend is None or not callable(getattr(backend, 'forceSlice', None)):
            raise RuntimeError('Активный движок не поддерживает forceSlice().')

        plate = app.getMultiBuildPlateModel().activeBuildPlate
        scene = app.getController().getScene()
        nodes = scene.getRoot().getAllChildren()
        if not any(n.getMeshData() is not None and n.callDecoration('isSliceable')
                   and n.callDecoration('getBuildPlateNumber') == plate for n in nodes):
            raise RuntimeError('На активном столе нет модели для нарезки.')

        self._disconnect()
        self.backend = backend

        # Останавливаем отложенный таймер изменения настроек (_change_timer), чтобы он не сбросил начатую нарезку
        change_timer = getattr(backend, '_change_timer', None)
        if change_timer is not None and callable(getattr(change_timer, 'stop', None)):
            try:
                change_timer.stop()
            except Exception:
                pass

        # Если слайсер уже запустил фоновую нарезку после изменения настроек, останавливаем старую перед чистым стартом
        if getattr(backend, '_slicing', False) and callable(getattr(backend, 'stopSlicing', None)):
            try:
                backend.stopSlicing()
            except Exception:
                pass

        # Очищаем старый G-code для активного стола, чтобы гарантировать ожидание свежего результата
        gcode_dict = getattr(scene, 'gcode_dict', None)
        if isinstance(gcode_dict, dict) and plate in gcode_dict:
            try:
                gcode_dict[plate] = []
            except Exception:
                pass

        self.job = {
            'status': 'starting',
            'progress': 0.0,
            'plate': plate,
            'started': False,
            'created': time.monotonic(),
            'error': None,
            'restarted_once': False
        }
        try:
            self._connect('slicingStarted', self._started)
            self._connect('backendStateChange', self._state)
            self._connect('processingProgress', self._progress)
            backend.forceSlice()
        except Exception as error:
            self.job['status'] = 'error'
            self.job['error'] = str(error)
            self._disconnect()
            raise
        return self.status()

    def _started(self, *args):
        if self.job:
            self.job['started'] = True
            self.job['status'] = 'processing'

    def _state(self, state):
        if not self.job:
            return
        if state == BackendState.Error:
            self.job['status'] = 'error'
            self.job['error'] = 'Движок сообщил об ошибке; подробности в журнале STE Slicer.'
        elif state == BackendState.Processing:
            self.job['started'] = True
            self.job['status'] = 'processing'
        elif state == BackendState.Done and self.job['started']:
            self.job['started'] = True
            self.job['status'] = 'done_signal'
        elif state == BackendState.NotStarted and self.job['started']:
            self.job['status'] = 'cancelled'

    def _progress(self, value):
        if self.job:
            val = float(value)
            self.job['progress'] = val
            if val > 0.0 and self.job['status'] == 'starting':
                self.job['started'] = True
                self.job['status'] = 'processing'
    def status(self):
        if not self.job:
            return {'status': 'not_started', 'gcode_ready': False}
        app = Application.getInstance()
        if app.getBackend() is not self.backend:
            self.job['status'] = 'error'
            self.job['error'] = 'Во время нарезки изменился активный движок.'

        scene = app.getController().getScene()
        lines = getattr(scene, 'gcode_dict', {}).get(self.job['plate'], [])
        has_gcode = bool(lines)

        backend_slicing = getattr(self.backend, '_slicing', False)
        if has_gcode and self.job['status'] in ('done_signal', 'completed'):
            self.job['status'] = 'completed'

        if self.job['status'] == 'starting' and time.monotonic() - self.job['created'] > 15:
            if backend_slicing:
                self.job['started'] = True
                self.job['status'] = 'processing'
            else:
                self.job['status'] = 'error'
                self.job['error'] = 'Движок не подтвердил начало нарезки.'

        ready = self.job['status'] == 'completed' and has_gcode
        result = {
            'status': self.job['status'],
            'progress': self.job['progress'],
            'gcode_ready': ready,
            'gcode_saved': False,
            'error': self.job['error']
        }
        if ready:
            if 'engine_estimates' not in self.job:
                self.job['engine_estimates'] = extract_estimates(lines if not isinstance(lines,str) else [lines])
            result['engine_estimates'] = self.job['engine_estimates']
        if self.job['status'] == 'done_signal' and not has_gcode:
            if time.monotonic() - self.job['created'] > 2.5 and not backend_slicing:
                result['status'] = 'error'
                result['error'] = 'Движок завершился, но G-code для активного стола отсутствует.'
        return result

    def cancel(self):
        if self.job and self.job['status'] in ('starting', 'processing', 'done_signal'):
            try:
                if self.backend is not None:
                    self.backend.stopSlicing()
            except Exception:
                pass
            self.job['status'] = 'cancelled'
        self._disconnect()
        return self.status()