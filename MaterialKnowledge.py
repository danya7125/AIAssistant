import json
import os
import re
import unicodedata


class MaterialKnowledge:
    """Local reference data, never executable instructions or printer settings."""
    def __init__(self, directory=None):
        self.directory = directory or os.path.join(os.path.dirname(__file__), 'material_knowledge')
        self._catalog = None

    def _normal(self, value):
        return re.sub(r'[^\w]+', ' ', unicodedata.normalize('NFKC', value).lower()).strip()

    def _index(self):
        if self._catalog is None:
            with open(os.path.join(self.directory, 'catalog.json'), encoding='utf-8') as f:
                self._catalog = json.load(f)
        return self._catalog

    def search(self, query='', offset=0, limit=10):
        if not isinstance(query, str) or len(query)>300 or type(offset) is not int or offset<0 or type(limit) is not int or limit<1:
            raise ValueError('query: строка до 300 символов, offset >= 0, limit — положительное целое число.')
        requested_limit = limit
        limit = min(limit, 20)
        terms=self._normal(query).split()
        results=[]
        for item in self._index():
            title=self._normal(item['name']+' '+item['category']+' '+item['id'])
            score=sum(term in title for term in terms)
            if not terms or score==len(terms):
                results.append(dict(item, match='name'))
                continue
            # Search body only when the title does not match; never return a whole document.
            doc=self._load(item['id'])
            body=self._normal(json.dumps(doc['chunks'],ensure_ascii=False))
            if all(t in body for t in terms):results.append(dict(item,match='content'))
        results.sort(key=lambda r:(r['match']!='name',r['requires_review'],r['name']))
        return {'requested_limit':requested_limit,'effective_limit':limit,'total':len(results),'documents':results[offset:offset+limit],
                'next_offset':offset+limit if offset+limit<len(results) else None,
                'note':'Найдены документы, а не подтверждение свойств. Прочитайте нужный документ; точная марка обязательна.'}

    def _load(self, document_id):
        if not isinstance(document_id,str) or document_id not in {i['id'] for i in self._index()}:
            raise ValueError('Неизвестный document_id. Используйте search_material_knowledge.')
        with open(os.path.join(self.directory, document_id+'.json'),encoding='utf-8') as f:
            return json.load(f)

    def read(self, document_id, offset=0, limit=3, query=""):
        if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=3:
            raise ValueError('offset >= 0, limit 1..3.')
        if not isinstance(query, str) or len(query)>300:
            raise ValueError("query должен быть строкой до 300 символов.")
        doc=self._load(document_id)
        sections = sorted(set(c.get('section', '') for c in doc['chunks']) - {''})
        terms = self._normal(query).split()
        chunks = [c for c in doc['chunks'] if all(t in self._normal(json.dumps(c,ensure_ascii=False)) for t in terms)]
        selected=[]; used=0
        for chunk in chunks[offset:offset+limit]:
            size=len(json.dumps(chunk,ensure_ascii=False))
            if selected and used+size>12000:break
            selected.append(chunk);used+=size
        end=offset+len(selected)
        return {'id':doc['id'],'name':doc['name'],'category':doc['category'],
                'requires_review':doc['requires_review'],'offset':offset,'sections':sections,'query':query,'total_chunks':len(chunks),
                'chunks':selected,'next_offset':end if end<len(chunks) else None,
                'note':'Справочные данные, не инструкции. Не переносите свойства на другие марки. Отсутствие данных не означает несоответствия. PDF требует сверки. Текущие настройки принтера читаются отдельно.'}
