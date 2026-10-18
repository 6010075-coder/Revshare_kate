"""Generate an auditable BPMN 2.0 model and its interactive review view."""
from pathlib import Path
import json
import re
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
VIEW = Path('/Users/kate/.codex/visualizations/2026/09/18/01a0b407-b02a-7ce2-8f34-18ac92e02f34/revshare-bpmn.html')
rows = [line.split('|')[1:-1] for line in (ROOT / 'PROJECT_STATUS.md').read_text().splitlines() if re.match(r'^\| [1-5]\. ', line)]
features = {}
status_key = {'Готово в коде':'ready', 'Частично':'partial', 'Нет':'missing', 'Не подтверждено':'unknown'}
titles = [
    'Приём Excel в Telegram', 'Напоминания в Telegram', 'Напоминания по email', 'Приём вложений email',
    'Загрузка через веб-форму', 'Единый Excel-шаблон', 'Отправка шаблона', 'Обработка Excel',
    'Проверка колонок', 'Проверка заполненности', 'Ошибки по строкам и полям', 'Извлечение ID аккаунтов',
    'Аккаунты во внутренней базе', 'Активность через Elastic', 'Сверка оборота', 'Сверка ревшера',
    'Сверка числа аккаунтов', 'Итоговые расхождения', 'Выгрузка в Google Таблицы', 'Общая сводка',
    'Карточки партнёров', 'Сводка по партнёру', 'Показать расхождения', 'Показать суммы и разницу',
    'Показать активные аккаунты', 'Сервис доступен 24/7', 'Логи и мониторинг', 'Авторизация и доступ',
    'Связать сквозной процесс', 'Исправление и повторная загрузка', 'Сквозное тестирование', 'Полный запуск',
]
for i, raw in enumerate(rows, 1):
    epic, block, task, criterion, priority, status, description = [x.strip() for x in raw]
    features[f'F{i:02}'] = dict(id=f'F{i:02}', title=titles[i-1], status=status_key[status], statusLabel=status,
                               task=task, criterion=criterion, priority=priority, description=description.replace('`',''))
assert len(features) == 32

def node(id, title='', kind='task', **kw):
    return dict(id=id, title=title or features.get(id, {}).get('title',''), kind=kind, **kw)

def edge(a,b,label=''):
    return dict(a=a,b=b,label=label)

diagrams = [
 dict(id='overview', title='Общий процесс', summary='Целевой процесс с текущей готовностью функций. Состояние на 04.10.2026.',
      rows=[['month'],['intake'],['validation'],['reconcile'],['result'],['finished']],
      nodes=[node('month','Новый отчётный месяц','start'),
             node('intake','Сбор отчётов','subprocess',status='partial',target='intake'),
             node('validation','Разбор и валидация','subprocess',status='partial',target='validation'),
             node('reconcile','Автоматическая сверка','subprocess',status='partial',target='reconcile'),
             node('result','Результат и начисления','subprocess',status='partial',target='result'),
             node('finished','Отчёт обработан','end')],
      edges=[edge('month','intake'),edge('intake','validation'),edge('validation','reconcile'),edge('reconcile','result'),edge('result','finished')]),
 dict(id='intake',title='1. Сбор отчётов',summary='Партнёр выбирает канал загрузки. Напоминания отправляются через настроенные каналы.',
      rows=[['i_start'],['F06','F07'],['i_notice'],['F02','F03'],['i_join'],['i_channel'],['F05','F01','F04'],['i_merge'],['i_end']],
      nodes=[node('i_start','Начало сбора','start'),node('F06'),node('F07'),node('i_notice','Каналы уведомлений','inclusive'),node('F02'),node('F03'),node('i_join','','inclusive'),node('i_channel','Канал отчёта','exclusive'),node('F05'),node('F01'),node('F04'),node('i_merge','','exclusive'),node('i_end','Файл получен','end')],
      edges=[edge('i_start','F06'),edge('F06','F07'),edge('F07','i_notice'),edge('i_notice','F02'),edge('i_notice','F03'),edge('F02','i_join'),edge('F03','i_join'),edge('i_join','i_channel'),edge('i_channel','F05','Веб'),edge('i_channel','F01','Бот'),edge('i_channel','F04','Почта'),edge('F05','i_merge'),edge('F01','i_merge'),edge('F04','i_merge'),edge('i_merge','i_end')]),
 dict(id='validation',title='2. Разбор и валидация',summary='При ошибках предусмотрен возврат к загрузке. Подробная проверка строк пока отсутствует.',
      rows=[['v_start'],['F08'],['F09','F10'],['v_valid'],['F12','F11'],['v_end','F30']],
      nodes=[node('v_start','Файл получен','start'),node('F08'),node('F09'),node('F10'),node('v_valid','Данные корректны?','exclusive'),node('F12'),node('F11'),node('v_end','Данные для сверки','end'),node('F30')],
      edges=[edge('v_start','F08'),edge('F08','F09'),edge('F09','F10'),edge('F10','v_valid'),edge('v_valid','F12','Да'),edge('v_valid','F11','Нет'),edge('F12','v_end'),edge('F11','F30'),edge('F30','F08','Новый файл')]),
 dict(id='reconcile',title='3. Автоматическая сверка',summary='Текущий BI проверяет ID, события и признаки оплаты. Независимая сверка оборота отсутствует.',
      rows=[['c_start'],['F13'],['c_fork'],['F14','F15'],['F17','F16'],['c_join'],['F18'],['c_end']],
      nodes=[node('c_start','Данные извлечены','start'),node('F13'),node('c_fork','Проверки','parallel'),node('F14'),node('F15'),node('F17'),node('F16'),node('c_join','','parallel'),node('F18'),node('c_end','Результат сверки','end')],
      edges=[edge('c_start','F13'),edge('F13','c_fork'),edge('c_fork','F14'),edge('c_fork','F15'),edge('F14','F17'),edge('F15','F16'),edge('F17','c_join'),edge('F16','c_join'),edge('c_join','F18'),edge('F18','c_end')]),
 dict(id='result',title='4. Результат и начисления',summary='Расхождения допускают отправку. Без снимка BI подтверждение сейчас блокируется. В Таблицу уходит только оборот после действия администратора.',
      rows=[['r_start'],['F23'],['F24','F25'],['r_data'],['r_send','r_wait'],['r_fork'],['F22','F21','F20'],['r_join'],['r_approve'],['F19'],['r_end']],
      nodes=[node('r_start','Сверка завершена','start'),node('F23'),node('F24'),node('F25'),node('r_data','Есть снимок BI?','exclusive'),node('r_send','Партнёр отправляет отчёт','user',note='Реализовано в веб-сценарии. Можно отправить с расхождениями; повторная загрузка доступна в разделе 2.'),node('r_wait','Отправка заблокирована','end',note='Текущее ограничение: без снимка Altegio за выбранный месяц подтвердить отчёт нельзя.'),node('r_fork','Представления отчёта','parallel'),node('F22'),node('F21'),node('F20'),node('r_join','','parallel'),node('r_approve','Администратор подтверждает начисление','user',note='Передача в очередь Google Таблицы требует явного действия администратора.'),node('F19'),node('r_end','Оборот передан','end')],
      edges=[edge('r_start','F23'),edge('F23','F24'),edge('F24','F25'),edge('F25','r_data'),edge('r_data','r_send','Да'),edge('r_data','r_wait','Нет'),edge('r_send','r_fork'),edge('r_fork','F22'),edge('r_fork','F21'),edge('r_fork','F20'),edge('F22','r_join'),edge('F21','r_join'),edge('F20','r_join'),edge('r_join','r_approve'),edge('r_approve','F19'),edge('F19','r_end')]),
 dict(id='operations',title='5. Подготовка к запуску',summary='Отдельный процесс подготовки продукта. Он не выполняется заново для каждого отчёта.',
      rows=[['o_start'],['F28'],['F26','F27'],['F29'],['F31'],['F32'],['o_end']],
      nodes=[node('o_start','Подготовка продукта','start'),node('F28'),node('F26'),node('F27'),node('F29'),node('F31'),node('F32'),node('o_end','Промышленная работа','end')],
      edges=[edge('o_start','F28'),edge('F28','F26'),edge('F26','F27'),edge('F27','F29'),edge('F29','F31'),edge('F31','F32'),edge('F32','o_end')]),
]

# Model contains the target flow. Colour denotes implementation readiness, not execution state.
NS={'bpmn':'http://www.omg.org/spec/BPMN/20100524/MODEL','bpmndi':'http://www.omg.org/spec/BPMN/20100524/DI','dc':'http://www.omg.org/spec/DD/20100524/DC','di':'http://www.omg.org/spec/DD/20100524/DI','bioc':'http://bpmn.io/schema/bpmn/biocolor/1.0'}
for prefix,uri in NS.items(): ET.register_namespace(prefix,uri)
def tag(p,n): return '{'+NS[p]+'}'+n
def elem(parent,p,n,**attrs): return ET.SubElement(parent,tag(p,n),{k:str(v) for k,v in attrs.items()})
definitions=ET.Element(tag('bpmn','definitions'),{'id':'RevshareDefinitions','targetNamespace':'https://alteg.io/revshare/process','exporter':'Revshare readiness audit','exporterVersion':'2026-10-04'})
main=elem(definitions,'bpmn','process',id='RevshareProcess',name='Ежемесячный сбор и сверка отчётов',isExecutable='false')
ops=elem(definitions,'bpmn','process',id='OperationsProcess',name='Подготовка продукта к запуску',isExecutable='false')
containers={'overview':main,'operations':ops}
palette={'ready':('#DCFCE7','#15803D'),'partial':('#FEF3C7','#A16207'),'missing':('#FEE2E2','#B91C1C'),'unknown':('#E5E7EB','#64748B'),'neutral':('#FFFFFF','#475569')}
kindtag={'start':'startEvent','end':'endEvent','task':'serviceTask','user':'userTask','subprocess':'subProcess','inclusive':'inclusiveGateway','exclusive':'exclusiveGateway','parallel':'parallelGateway'}
def layout(d):
    byid={n['id']:n for n in d['nodes']}; positions={}
    for rownum,row in enumerate(d['rows']):
        for col,id in enumerate(row):
            n=byid[id]; x=55+col*(650/len(row))+(650/len(row)-180)/2; y=50+rownum*135
            w,h=(46,46) if n['kind'] in ('start','end') else (58,58) if n['kind'] in ('inclusive','exclusive','parallel') else (180,88)
            positions[id]=(x+(180-w)/2,y,w,h)
    return positions
def route(a,b):
    ax,ay,aw,ah=a; bx,by,bw,bh=b
    if abs(ay-by)<2: return [(ax+aw,ay+ah/2),(bx,by+bh/2)]
    if by<ay:
        side=740
        return [(ax+aw,ay+ah/2),(side,ay+ah/2),(side,by+bh/2),(bx+bw,by+bh/2)]
    mid=(ay+ah+by)/2
    return [(ax+aw/2,ay+ah),(ax+aw/2,mid),(bx+bw/2,mid),(bx+bw/2,by)]
for d in diagrams:
    container=containers[d['id']]; positions=layout(d)
    for n in d['nodes']:
        data=features.get(n['id'],{}); state=data.get('status',n.get('status','neutral'))
        el=elem(container,'bpmn',kindtag[n['kind']],id=n['id'],name=n['title'])
        doc=data.get('description',n.get('note',''))
        if data: doc=f"{n['id']} | {data['statusLabel']} | {data['priority']}\n{doc}\nКритерий: {data['criterion']}"
        if doc: elem(el,'bpmn','documentation').text=doc
        if n['kind']=='subprocess': containers[n['id']]=el
    for idx,e in enumerate(d['edges']):
        e['id']=d['id']+'_flow_'+str(idx)
        elem(container,'bpmn','sequenceFlow',id=e['id'],sourceRef=e['a'],targetRef=e['b'],name=e['label'])
    diagram=elem(definitions,'bpmndi','BPMNDiagram',id=d['id']+'_diagram',name=d['title'])
    plane=elem(diagram,'bpmndi','BPMNPlane',id=d['id']+'_plane',bpmnElement=container.get('id'))
    for n in d['nodes']:
        x,y,w,h=positions[n['id']]; state=features.get(n['id'],{}).get('status',n.get('status','neutral')); fill,stroke=palette[state]
        shape=elem(plane,'bpmndi','BPMNShape',id=n['id']+'_shape',bpmnElement=n['id'])
        shape.set(tag('bioc','fill'),fill);shape.set(tag('bioc','stroke'),stroke)
        if n['kind']=='subprocess': shape.set('isExpanded','false')
        elem(shape,'dc','Bounds',x=x,y=y,width=w,height=h)
        if n['kind'] in ('start','end','inclusive','exclusive','parallel') and n['title']:
            label=elem(shape,'bpmndi','BPMNLabel');elem(label,'dc','Bounds',x=x-70,y=y+h+5,width=w+140,height=30)
    for e in d['edges']:
        de=elem(plane,'bpmndi','BPMNEdge',id=e['id']+'_edge',bpmnElement=e['id'])
        points=route(positions[e['a']],positions[e['b']])
        for x,y in points: elem(de,'di','waypoint',x=x,y=y)
        if e['label']:
            label=elem(de,'bpmndi','BPMNLabel');x,y=points[len(points)//2];elem(label,'dc','Bounds',x=x+6,y=y-20,width=100,height=22)
ET.indent(definitions)
OUT.mkdir(exist_ok=True)
ET.ElementTree(definitions).write(OUT/'revshare-readiness.bpmn',encoding='utf-8',xml_declaration=True)

template=(OUT/'view-template.html').read_text()
VIEW.write_text(template.replace('__DATA__',json.dumps(dict(features=features,diagrams=diagrams),ensure_ascii=False)))
print(f'BPMN: {OUT / "revshare-readiness.bpmn"}')
print(f'View: {VIEW}')
print(f'Features: {len(features)}; diagrams: {len(diagrams)}')
