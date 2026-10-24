"""
AutoExport module for SteSlicer AIAssistant plugin.
Automatically exports sliced G-Code and 3MF workspace to AIAssistant/output/<ModelName>/
"""
import os
import re
from UM.Application import Application
from UM.Logger import Logger
from UM.Mesh.MeshWriter import MeshWriter
from UM.Workspace.WorkspaceWriter import WorkspaceWriter


def clean_filename(name):
    """Sanitize folder and file names for Windows filesystem."""
    if not name or not isinstance(name, str):
        return "Model"
    cleaned = re.sub(r'[\\/*?:"<>|]', '_', name.strip())
    cleaned = cleaned.strip('. ')
    return cleaned if cleaned else "Model"


def get_output_base_dir():
    plugin_dir = os.path.dirname(os.path.abspath(__file__))
    output_root = os.path.join(plugin_dir, "output")
    if not os.path.exists(output_root):
        try:
            os.makedirs(output_root, exist_ok=True)
        except Exception:
            pass
    return output_root


def auto_export_project_and_gcode():
    app = Application.getInstance()
    scene = app.getController().getScene()
    root = scene.getRoot()
    plate = app.getMultiBuildPlateModel().activeBuildPlate
    
    # Check if G-code is ready
    gcode_dict = getattr(scene, 'gcode_dict', None)
    if not isinstance(gcode_dict, dict) or plate not in gcode_dict or not gcode_dict[plate]:
        return {'ok': False, 'error': 'G-code не сформирован для активного стола.'}
        
    # Чтение текущих настроек для записи в G-code
    global_stack = app.getGlobalContainerStack()
    extruder_stack = app.getExtruderManager().getActiveExtruderStack()
    active_stack = extruder_stack if extruder_stack else global_stack

    def get_setting(key):
        if active_stack:
            val = active_stack.getProperty(key, 'value')
            return str(val) if val is not None else "Авто"
        return "Неизвестно"
        
    material_name = getattr(active_stack, 'material', None)
    material_str = material_name.getName() if material_name else "Не определен"
    
    # Get active models and determine project/model name
    sliceable_nodes = [
        n for n in root.getAllChildren()
        if n.getMeshData() is not None
        and n.callDecoration('isSliceable')
        and n.callDecoration('getBuildPlateNumber') == plate
    ]
    
    if sliceable_nodes:
        raw_name = sliceable_nodes[0].getName()
    else:
        raw_name = "Model"
        
    base_name = os.path.splitext(raw_name)[0]
    model_name = clean_filename(base_name)
    folder_name = model_name
    
    output_root = get_output_base_dir()
    target_dir = os.path.join(output_root, folder_name)
    
    try:
        os.makedirs(target_dir, exist_ok=True)
    except Exception as e:
        return {'ok': False, 'error': 'Не удалось создать папку для экспорта: ' + str(e)}
        
    saved_files = []
    errors = []
    
    # 1. Export G-Code
    gcode_path = os.path.join(target_dir, "{0}.gcode".format(model_name))
    try:
        with open(gcode_path, "wt", encoding="utf-8") as stream:
            # Записываем информационный заголовок от ИИ с параметрами печати
            stream.write("; ==================================================================\n")
            stream.write("; Сгенерировано при участии AI Assistant STE Slicer\n")
            stream.write("; Проект: {0}\n".format(model_name))
            stream.write("; \n")
            stream.write("; ПАРАМЕТРЫ ПЕЧАТИ:\n")
            stream.write("; Материал: {0}\n".format(material_str))
            stream.write("; Высота слоя: {0} мм\n".format(get_setting('layer_height')))
            stream.write("; Плотность заполнения: {0}%\n".format(get_setting('infill_sparse_density')))
            stream.write("; Температура сопла: {0} °C\n".format(get_setting('material_print_temperature')))
            stream.write("; Температура стола: {0} °C\n".format(get_setting('material_bed_temperature')))
            stream.write("; Скорость печати: {0} мм/с\n".format(get_setting('speed_print')))
            stream.write("; \n")
            stream.write("; ИНФОРМАЦИЯ ДЛЯ ПОЛЬЗОВАТЕЛЯ:\n")
            stream.write("; Этот файл содержит скомпилированные инструкции для 3D/5D-принтера.\n")
            stream.write("; Он отвечает за координаты перемещения печатающей головки,\n")
            stream.write("; подачу материала и жестко фиксирует температуры.\n")
            stream.write("; ==================================================================\n\n")

            gcode_data = gcode_dict[plate]
            if isinstance(gcode_data, str):
                stream.write(gcode_data)
            else:
                for chunk in gcode_data:
                    stream.write(chunk)
        saved_files.append(gcode_path)
    except Exception as e:
        errors.append("Ошибка сохранения G-Code: " + str(e))
        
    # 2. Export 3MF Workspace
    threemf_path = os.path.join(target_dir, "{0}.3mf".format(model_name))
    try:
        workspace_writer = app.getWorkspaceFileHandler().getWriter("3MFWorkspaceWriter")
        if workspace_writer is not None:
            with open(threemf_path, "wb") as stream:
                success = workspace_writer.write(stream, [root], WorkspaceWriter.OutputMode.BinaryMode)
                if success is not False:
                    saved_files.append(threemf_path)
                else:
                    errors.append("3MFWorkspaceWriter вернул False.")
        else:
            mesh_3mf_writer = app.getMeshFileHandler().getWriter("3MFWriter")
            if mesh_3mf_writer is not None:
                with open(threemf_path, "wb") as stream:
                    mesh_3mf_writer.write(stream, [root], MeshWriter.OutputMode.BinaryMode)
                saved_files.append(threemf_path)
    except Exception as e:
        errors.append("Ошибка сохранения 3MF: " + str(e))
        
    return {
        'ok': len(saved_files) > 0,
        'target_dir': target_dir,
        'saved_files': saved_files,
        'model_name': model_name,
        'errors': errors
    }