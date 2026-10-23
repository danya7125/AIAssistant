import json
import os
import re
import math


class MaterialCards:
    KEYS = ('material_print_temperature', 'material_bed_temperature', 'layer_height',
            'machine_nozzle_size', 'speed_print', 'retraction_amount', 'retraction_speed')
    def __init__(self, directory=None):
        self.directory = directory or os.path.join(os.path.dirname(__file__), 'material_cards')

    def normal(self, text):
        return re.sub(r'[^a-zа-я0-9]', '', text.lower().replace('ё','е'))

    def get(self, material):
        if not isinstance(material, str) or not material or len(material)>200:
            raise ValueError('Укажите точную марку материала.')
        with open(os.path.join(self.directory,'catalog.json'),encoding='utf-8') as f:
            catalog=json.load(f)
        query=self.normal(material)
        matches=[d for d in catalog if query in (self.normal(d['name']),self.normal(d['category']+' '+d['name']),self.normal(d['id']))]
        if len(matches)!=1:
            return {'found':False,'reason':'Марка не найдена однозначно. Используйте точное имя с серией или ID из поиска.', 'candidates':matches}
        with open(os.path.join(self.directory,matches[0]['id']+'.json'),encoding='utf-8') as f:
            result=json.load(f)
        result['found']=True
        result['note']='Полная карточка доступных разделов. Отсутствие свойства означает неизвестность, не несоответствие. Рекомендации не подтверждают ресурс детали. Настройки автоматически не изменяются.'
        return result

    def compare(self, card, values):
        checks=[]
        if not card.get('found'):return checks
        labels=(('температура сопла','material_print_temperature'),('температура стола','material_bed_temperature'),
                ('высота слоя','layer_height'),('диаметр сопла','machine_nozzle_size'),
                ('скорость печати','speed_print'),('скорость ретракта','retraction_speed'),('ретракт','retraction_amount'))
        for section in card['sections']:
            if 'параметры печати' not in section['heading'].lower():continue
            for block in section['content']:
                if not isinstance(block,dict) or 'table' not in block:continue
                for row in block['table']:
                    if len(row)<2 or not all(isinstance(c,str) for c in row):continue
                    label=row[-2].lower(); raw=row[-1]
                    key=next((k for phrase,k in labels if phrase in label),None)
                    if key is None:continue
                    current=values.get(key)
                    item={'setting':key,'current':current,'recommendation':raw,'status':'manual_review'}
                    # Only unambiguous numeric values/ranges are automatically evaluated.
                    clean=raw.replace(',','.').strip()
                    match=re.fullmatch(r'(от\s*)?([0-9]+(?:\.[0-9]+)?)(?:\s*[-–]\s*([0-9]+(?:\.[0-9]+)?))?\s*(?:°\s*[CС]|мм/с|мм)?',clean)
                    if type(current) not in (int,float) or not math.isfinite(current):item['status']='current_unavailable'
                    elif match:
                        lo=float(match.group(2)); hi=float(match.group(3)) if match.group(3) else (None if match.group(1) else lo)
                        item['status']='below_recommendation' if current<lo else ('above_recommendation' if hi is not None and current>hi else 'within_recommendation')
                    else:
                        # Multiple conditional ranges: only diagnose outside ALL ranges.
                        ranges=re.findall(r'([0-9]+(?:\.[0-9]+)?)\s*[-–]\s*([0-9]+(?:\.[0-9]+)?)\s*°\s*[CС]',clean)
                        if key=='material_print_temperature' and ranges:
                            lows=[float(a) for a,b in ranges]; highs=[float(b) for a,b in ranges]
                            if current<min(lows):item['status']='below_all_conditional_ranges'
                            elif current>max(highs):item['status']='above_all_conditional_ranges'
                    checks.append(item)
        return checks
