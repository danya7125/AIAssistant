"""Scene tools. All methods run on the Qt thread; coordinates are X/Y(up)/Z."""
import copy
import math
import os
import numpy as np
from UM.Application import Application
from UM.Math.Vector import Vector
from UM.Math.Quaternion import Quaternion
from UM.Scene.SceneNode import SceneNode
from UM.Operations.Operation import Operation


class SceneEdit(Operation):
    def __init__(self, edits):
        super().__init__()
        self.edits = edits
    def undo(self):
        for node, old, new in self.edits:
            node.setTransformation(old)
    def redo(self):
        for node, old, new in self.edits:
            node.setTransformation(new)


class SceneTools:
    def __init__(self):
        self.pending = {}
        self.jobs = []
        self.dirty = False
    def app(self):
        return Application.getInstance()
    def nodes(self):
        app = self.app()
        root = app.getController().getScene().getRoot()
        plate = app.getMultiBuildPlateModel().activeBuildPlate
        return [n for n in root.getAllChildren() if n.getMeshData() is not None
                and n.callDecoration('isSliceable') and n.callDecoration('getBuildPlateNumber') == plate]
    def points(self, node):
        m = node.getWorldTransformation().getData()
        v = node.getMeshData().getVertices()
        if len(v) > 1500000:
            raise ValueError('Сетка слишком большая для анализа ориентации.')
        return v.dot(m[:3,:3].T) + m[:3,3]
    def get_scene_models(self):
        models = []
        for n in self.nodes():
            v = self.points(n)
            models.append({'model_id':str(id(n)), 'name':n.getName(),
                           'size_xyz_y_up_mm':(v.max(0)-v.min(0)).tolist(),
                           'position_xyz_y_up_mm':v.min(0).tolist()})
        return {'models':models, 'loads':dict(self.pending), 'coordinates':'X/Z = table, Y = height'}
    def ready(self):
        if any(v == 'loading' for v in self.pending.values()):
            raise ValueError('Загрузка ещё идёт; повторите get_scene_models перед изменением или нарезкой.')
    def classic(self):
        self.ready()
        if self.app().getGlobalContainerStack().getProperty('printing_mode','value') != 'classic':
            raise ValueError('Поворот и расстановка доступны только в classic/3D. В 5D нужна отдельная проверка оси и основания.')
    def node(self, model_id):
        self.classic()
        found = [n for n in self.nodes() if str(id(n)) == str(model_id)]
        if len(found) != 1:
            raise ValueError('Получите актуальный model_id через get_scene_models.')
        n = found[0]
        if n.getParent() is not self.app().getController().getScene().getRoot():
            raise ValueError('Модели внутри групп пока не поддерживаются; группа не изменена.')
        return n
    def load_model(self, path):
        from UM.Mesh.ReadMeshJob import ReadMeshJob
        if not isinstance(path,str) or not os.path.isabs(path) or not os.path.isfile(path):
            raise ValueError('Нужен существующий абсолютный путь к локальному файлу.')
        path = os.path.normpath(path)
        if os.path.splitext(path)[1].lower() not in ('.stl','.obj'):
            raise ValueError('Поддерживаются STL и OBJ; файлы проектов не загружаются.')
        if path in self.pending:
            return {'path':path,'status':self.pending[path], 'note':'Повторная загрузка не выполнялась.'}
        app = self.app()
        if app.getGlobalContainerStack() is None:
            raise ValueError('Сначала выберите принтер.')
        printer_id = app.getGlobalContainerStack().getId()
        plate = app.getMultiBuildPlateModel().activeBuildPlate
        job = ReadMeshJob(path)
        self.pending[path] = 'loading'
        self.jobs.append(job)
        def finished(j):
            def apply():
                try:
                    current = app.getGlobalContainerStack()
                    if current is None or current.getId() != printer_id or app.getMultiBuildPlateModel().activeBuildPlate != plate:
                        raise ValueError('Во время загрузки изменился принтер или активный стол; модель не добавлена.')
                    before_ids = {id(n) for n in self.nodes()}
                    if not j.getResult():
                        raise ValueError('Файл не содержит модели.')
                    app._currently_loading_files.append(path)
                    app._readMeshFinished(j)
                    if not any(id(n) not in before_ids for n in self.nodes()):
                        raise ValueError('Штатный загрузчик не добавил модель на активный стол.')
                    self.pending[path] = 'completed'
                    self.dirty = True
                except Exception as error:
                    self.pending[path] = 'failed: ' + str(error)
                finally:
                    if path in app._currently_loading_files:
                        app._currently_loading_files.remove(path)
                    self.jobs.remove(j)
            app.callLater(apply)
        job.finished.connect(finished)
        job.start()
        return {'status':'loading','path':path,'note':'Дождитесь completed через get_scene_models; это ещё не подтверждение загрузки.'}
    def orientation_snapshot(self, model_id):
        n = self.node(model_id)
        v = self.points(n)
        ix = n.getMeshData().getIndices()
        faces = v.reshape((-1,3,3)) if ix is None else v[np.asarray(ix).reshape((-1,3))]
        stack = self.app().getGlobalContainerStack()
        values = {}
        # Use the selected object's effective stack, including material/extruder overrides.
        effective = n.callDecoration('getStack') or stack
        for key in ('layer_height','layer_height_0','support_angle'):
            values[key] = effective.getProperty(key,'value')
        _,lo,hi = self.build_bounds()
        values.update({'faces':faces.copy(),'model_id':model_id,'build_size':(hi-lo).tolist()})
        return values
    def build_bounds(self):
        volume = self.app().getBuildVolume()
        bounds = volume.getBoundingBox()
        if bounds is None:
            raise ValueError('Рабочая область ещё не готова. Размещение не выполнено.')
        lo = np.array([bounds.minimum.x, bounds.minimum.y, bounds.minimum.z], dtype=float)
        hi = np.array([bounds.maximum.x, bounds.maximum.y, bounds.maximum.z], dtype=float)
        if not np.isfinite(lo).all() or not np.isfinite(hi).all() or np.any(hi <= lo):
            raise ValueError('Некорректные границы рабочей области.')
        return volume, lo, hi
    def check_placement(self, nodes):
        volume, lo, hi = self.build_bounds()
        for n in nodes:
            v = self.points(n)
            if not np.isfinite(v).all() or np.any(v.min(0) < lo - .001) or np.any(v.max(0) > hi + .001):
                raise ValueError('Модель выходит за рабочую область: ' + n.getName() + '. Изменения отменены.')
            volume.checkBoundsAndUpdate(n)
            if n.isOutsideBuildArea():
                raise ValueError('STE Slicer отклонил положение модели (границы, запрещённая зона или экструдер): ' + n.getName() + '. Изменения отменены.')
    def commit(self, nodes, edit):
        old = [copy.deepcopy(n.getLocalTransformation()) for n in nodes]
        try:
            edit()
            self.check_placement(nodes)
            new = [copy.deepcopy(n.getLocalTransformation()) for n in nodes]
        finally:
            for n,t in zip(nodes,old):
                n.setTransformation(t)
                self.app().getBuildVolume().checkBoundsAndUpdate(n)
        SceneEdit(list(zip(nodes,old,new))).push()
        for n in nodes:
            self.app().getBuildVolume().checkBoundsAndUpdate(n)
        self.dirty = True
        return {'changes':[{'key':'model_transform: ' + n.getName(),'before':a.getData().tolist(),'after':b.getData().tolist()} for n,a,b in zip(nodes,old,new)],
                'scene':self.get_scene_models(),'note':'Изменение можно отменить штатным Undo. Проверка геометрии обязательна перед нарезкой.'}
    def orient_model(self, model_id, axis, degrees):
        n = self.node(model_id)
        if axis not in ('x','y','z') or isinstance(degrees,bool) or not isinstance(degrees,(int,float)) or not math.isfinite(degrees) or abs(degrees)>360:
            raise ValueError('Ось x/y/z, угол от -360 до 360 градусов.')
        vector = {'x':Vector(1,0,0),'y':Vector(0,1,0),'z':Vector(0,0,1)}[axis]
        volume, low, high = self.build_bounds()
        original = self.points(n)
        center = (original.min(0) + original.max(0)) / 2
        def edit():
            n.rotate(Quaternion.fromAngleAxis(math.radians(degrees),vector),SceneNode.TransformSpace.World)
            v = self.points(n)
            lo, hi = v.min(0), v.max(0)
            half = (hi-lo)/2
            if np.any(hi-lo > high-low):
                raise ValueError('После поворота модель не помещается; изменения отменены.')
            target = (low+high)/2 if len(self.nodes()) == 1 else np.maximum(low+half, np.minimum(center, high-half))
            delta = target - (lo+hi)/2
            delta[1] = low[1]-lo[1]
            n.translate(Vector(*delta.tolist()),SceneNode.TransformSpace.World)
        return self.commit([n],edit)
    def arrange_models(self, margin_mm=10):
        self.classic()
        if isinstance(margin_mm,bool) or not isinstance(margin_mm,(int,float)) or not math.isfinite(margin_mm) or not 2<=margin_mm<=100:
            raise ValueError('margin_mm: от 2 до 100 мм.')
        nodes = self.nodes()
        if not nodes:
            raise ValueError('Нет моделей.')
        for n in nodes:
            self.node(str(id(n)))
        stack = self.app().getGlobalContainerStack()
        if stack.getProperty('machine_shape','value') != 'rectangular':
            raise ValueError('Авторасстановка пока поддерживает только прямоугольный стол.')
        volume, low, high = self.build_bounds()
        width,height,depth = high-low
        boxes = [(n,self.points(n).min(0),self.points(n).max(0)) for n in nodes]
        boxes.sort(key=lambda b:-(b[2][2]-b[1][2]))
        x=z=margin_mm; row=0; placements=[]
        for n,lo,hi in boxes:
            sx,sy,sz=hi-lo
            if sx+2*margin_mm>width or sz+2*margin_mm>depth or sy>height:
                raise ValueError('Модель не помещается в область печати; сцена не изменена.')
            if x+sx+margin_mm>width:
                x=margin_mm; z+=row+margin_mm; row=0
            if z+sz+margin_mm>depth:
                raise ValueError('Все модели не помещаются с заданным зазором; сцена не изменена.')
            placements.append((n,Vector(x+low[0]-lo[0],low[1]-lo[1],z+low[2]-lo[2])))
            x+=sx+margin_mm; row=max(row,sz)
        if len(boxes) == 1:
            n, lo, hi = boxes[0]
            center = (low+high)/2
            placements = [(n,Vector(center[0]-(lo[0]+hi[0])/2, low[1]-lo[1], center[2]-(lo[2]+hi[2])/2))]
        def edit():
            for n,d in placements:
                n.translate(d,SceneNode.TransformSpace.World)
        result=self.commit(nodes,edit)
        result['not_checked']=['фактические траектории поддержек и каймы','последовательная печать']
        return result
