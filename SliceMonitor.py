import time
from UM.Application import Application
from UM.Backend.Backend import BackendState


class SliceMonitor:
    """Tracks only a new slice started by this assistant, on the Qt thread."""
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
        if self.job and self.job['status'] in ('starting', 'processing'):
            raise RuntimeError('Нарезка уже выполняется. Проверьте её статус.')
        backend = app.getBackend()
        if backend is None or not callable(getattr(backend, 'forceSlice', None)):
            raise RuntimeError('Активный движок не поддерживает forceSlice().')
        if getattr(backend, '_slicing', False):
            raise RuntimeError('Слайсер уже выполняет другую нарезку.')
        plate = app.getMultiBuildPlateModel().activeBuildPlate
        scene = app.getController().getScene()
        nodes = scene.getRoot().getAllChildren()
        if not any(n.getMeshData() is not None and n.callDecoration('isSliceable')
                   and n.callDecoration('getBuildPlateNumber') == plate for n in nodes):
            raise RuntimeError('На активном столе нет модели для нарезки.')
        self._disconnect()
        self.backend = backend
        self.job = {'status': 'starting', 'progress': 0.0, 'plate': plate,
                    'started': False, 'created': time.monotonic(), 'error': None}
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
        self.job['started'] = True
        self.job['status'] = 'processing'

    def _state(self, state):
        if not self.job:
            return
        if state == BackendState.Error:
            self.job['status'] = 'error'
            self.job['error'] = 'Движок сообщил об ошибке; подробности в журнале STE Slicer.'
        elif state == BackendState.Processing:
            self.job['status'] = 'processing'
        elif state == BackendState.Done and self.job['started']:
            self.job['status'] = 'done_signal'
        elif state == BackendState.NotStarted and self.job['started']:
            self.job['status'] = 'cancelled'

    def _progress(self, value):
        if self.job:
            self.job['progress'] = float(value)

    def status(self):
        if not self.job:
            return {'status': 'not_started', 'gcode_ready': False}
        app = Application.getInstance()
        if app.getBackend() is not self.backend:
            self.job['status'] = 'error'
            self.job['error'] = 'Во время нарезки изменился активный движок.'
        if self.job['status'] == 'starting' and time.monotonic() - self.job['created'] > 10:
            self.job['status'] = 'error'
            self.job['error'] = 'Движок не подтвердил начало нарезки.'
        scene = app.getController().getScene()
        lines = getattr(scene, 'gcode_dict', {}).get(self.job['plate'], [])
        ready = self.job['status'] in ('done_signal', 'completed') and bool(lines)
        if ready:
            self.job['status'] = 'completed'
        result = {'status': self.job['status'], 'progress': self.job['progress'],
                  'gcode_ready': ready, 'gcode_saved': False,
                  'error': self.job['error']}
        if self.job['status'] == 'done_signal' and not lines:
            result['status'] = 'error'
            result['error'] = 'Движок завершился, но G-code для активного стола отсутствует.'
        return result

    def cancel(self):
        if self.job and self.job['status'] in ('starting', 'processing'):
            self.backend.stopSlicing()
            self.job['status'] = 'cancelled'
        return self.status()
