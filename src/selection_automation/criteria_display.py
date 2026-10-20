"""Chinese parameter dimensions, conservative aliases, and a short first view."""
from copy import deepcopy
import re

VERSION='chinese-parameter-dimensions-v3'

# Only equivalent parameter names share a key. Native/maximum resolution,
# screen diagonal/body dimensions, and connection technology/ports stay apart.
GROUPS=[
 ('screen_size','屏幕尺寸',10,'screen size|display size|standing screen display size|screen diagonal|display diagonal|屏幕尺寸|屏幕大小'),
 ('dimensions','商品长宽高',35,'item dimensions d x w x h|product dimensions|item dimensions l x w x h|item dimensions|dimensions|商品尺寸|长宽高'),
 ('display_type','显示类型',28,'display type|显示类型'),
 ('product_type','商品类型',70,'item type name|product type|form factor|商品类型'),
 ('aspect_ratio','屏幕比例',32,'aspect ratio|display aspect ratio|屏幕比例'),
 ('screen_finish','屏幕表面处理',70,'screen finish|screen surface description|屏幕表面处理'),
 ('contrast','对比度',60,'contrast ratio|display contrast ratio|对比度'),
 ('refresh_rate','刷新率',18,'refresh rate|screen refresh rate|display refresh rate|刷新率'),
 ('display_technology','显示技术',30,'display technology|panel technology|显示技术'),
 ('viewing_angle','可视角度',75,'viewing angle|可视角度'),
 ('brightness','亮度',55,'brightness|display brightness|亮度'),
 ('pixel_pitch','像素间距',80,'pixel pitch|像素间距'),
 ('color_gamut','色域',80,'color gamut|色域'),
 ('picture_enhancement','画质增强技术',90,'picture quality enhancement technology|画质增强技术'),
 ('resolution','分辨率',12,'resolution|screen resolution|分辨率'),
 ('maximum_resolution','最高支持分辨率',65,'display resolution maximum|maximum display resolution|maximum resolution|最高支持分辨率'),
 ('native_resolution','原生分辨率',14,'native resolution|native display resolution|原生分辨率'),
 ('connectivity','连接方式',45,'connectivity technology|connection technology|连接方式'),
 ('interfaces','连接接口',25,'hardware connectivity|input interface|连接接口'),
 ('adaptive_sync','自适应同步',80,'adaptive sync|自适应同步'),
 ('features','附加功能',90,'additional features|special features|附加功能'),
 ('uses','使用场景',85,'specific uses for product|recommended uses for product|使用场景'),
 ('color','颜色',75,'color|colour|颜色'),
 ('brand','品牌',90,'brand|brand name|品牌'),
 ('model_number','型号编号',50,'model number|item model number|型号编号'),
 ('model_name','型号名称',60,'model name|型号名称'),
 ('manufacturer','制造商',95,'manufacturer|制造商'),
 ('components','随附配件',55,'included components|included accessories|随附配件'),
 ('power','功耗',80,'power consumption|功耗'),
 ('voltage','电压',80,'voltage|电压'),
 ('response_time','响应时间',22,'response time|响应时间'),
 ('hdmi_ports','接口数量（高清接口）',80,'total number of hdmi ports|number of hdmi ports|hdmi ports|接口数量（高清接口）'),
 ('mounting','安装方式',24,'mounting type|mount type|installation type|安装方式'),
 ('compatibility','兼容设备',85,'compatible devices|compatibility|兼容设备'),
 ('shape','外形',50,'shape|外形'),
 ('unit_count','包装数量',65,'unit count|number of items|item package quantity|包装数量'),
 ('series','系列编号',98,'series number|series|系列编号'),
 ('spare_parts','备件供应年限',100,'eu spare part availability duration|备件供应年限'),
 ('gtin','全球贸易商品编码',100,'global trade identification number|gtin|全球贸易商品编码'),
 ('component_outputs','分量视频输出数量',98,'number of component outputs|分量视频输出数量'),
 ('usb_ports','通用串行接口数量',90,'total usb ports|number of usb ports|通用串行接口数量'),
 ('usb3_ports','高速串行接口数量',92,'total usb 3.0 ports|number of usb 3.0 ports|高速串行接口数量'),
 ('weight','重量',80,'item weight|product weight|weight|重量'),
 ('weight_capacity','最大称重',20,'weight capacity maximum|maximum weight capacity|maximum weight|weight capacity|maximum load|最大称重|最大承重|称重上限'),
 ('readout_accuracy','读数精度',25,'readout accuracy|weighing accuracy|读数精度|称重精度'),
 ('scale_type','秤型',30,'weigh scale type|scale type|秤型|秤类型'),
 ('battery_type','电池类型',45,'battery type|电池类型'),
 ('measurement_type','计量方式',45,'measurement type|计量方式|计量方式/单位|测量单位'),
 ('material','材质',30,'material|material type|材质'),
 ('capacity','容量',25,'capacity|容量'),
 ('wash_capacity','洗衣容量',25,'wash capacity|washing capacity|washer capacity|capacity class|wash capacity lbs|洗衣容量|洗衣机容量|洗衣机容量(磅)|容量档位'),
 ('dry_capacity','烘干容量',40,'dry capacity|dryer capacity|烘干容量|烘干机容量'),
 ('loading_direction','装载方式',40,'loading direction|load type|loading type|取放方式|装载方式'),
 ('wash_programs','洗涤程序数量',55,'wash program count|wash cycle count|洗涤程序数量'),
 ('foldable','可折叠',50,'foldable design|是否可折叠|可折叠'),
 ('control_type','控制方式',50,'control type|控制方式'),
 ('heating_zones','加热区数量',30,'number of heating elements|number of burners|burner count|burner_count|heating zone count|加热区数量|炉头数量'),
 ('size','尺寸',20,'size|尺寸'),
 ('power_source','供电方式',35,'power source|power supply|供电方式'),
 ('style','款式',55,'style|款式'),
 ('pattern','图案',60,'pattern|图案'),
 ('finish','表面处理',75,'finish type|finish|表面处理'),
]


def key(value):return re.sub(r'[^a-z0-9\u4e00-\u9fff]','',str(value).casefold())
ALIASES={key(alias):(identity,label,rank) for identity,label,rank,aliases in GROUPS for alias in aliases.split('|')}
NON_MATCH_PARAMETER=re.compile(
    r'^(?:brand(?:\s*name)?|manufacturer|department|date\s*first\s*available|'
    r'ASIN|UPC|EAN|GTIN|global\s*trade\s*identification\s*number|'
    r'EU\s*spare\s*part\s*availability\s*duration|品牌|制造商|生产厂家|'
    r'适用部门|首次上架日期|全球贸易商品编码|备件供应年限)(?:\b|[\s:：]|$)',re.I)
_GENERIC_LABELS={'其他商品参数','原始参数','自定义条件'}


def _clean_label(value):
    return re.sub(r'[\u200e\u200f\u202a-\u202e\u2066-\u2069]','',str(value)).strip()


def _descriptor(item):
    # Source labels carry provenance. A paragraph or combined list of several
    # parameters must not become a new, independent comparison dimension.
    names=[item.get('label',''),item.get('label_zh',''),*(item.get('source_labels') or [])]
    for name in names:
        name=_clean_label(name)
        exact=ALIASES.get(key(name))
        if exact:return exact
        prefix=re.split(r'[:：]',name,maxsplit=1)[0].strip()
        if prefix!=name and key(prefix) in ALIASES:return ALIASES[key(prefix)]
    return None


def _chinese_label(item):
    for value in (item.get('label_zh'),item.get('label')):
        if isinstance(value,str) and re.search(r'[\u4e00-\u9fff]',value) and value not in _GENERIC_LABELS:
            return value
    return None


def organize(catalog):
    result=deepcopy(catalog);groups={};items=[]
    raw={str(row.get('id') or key(row.get('label',''))):row for row in result.get('raw_parameters',[])}
    for item in result.get('items',[]):
        if item.get('kind')!='text':items.append(item);continue
        descriptor=_descriptor(item)
        names=[item.get('label',''),*(item.get('source_labels') or [])]
        irrelevant=any(NON_MATCH_PARAMETER.search(_clean_label(name)) for name in names)
        combined=not descriptor and any(re.search(r'[:：]',_clean_label(name)) for name in names)
        if not item.get('selected') and (irrelevant or combined):
            raw[str(item.get('id') or key(item.get('label','')))]=item
            continue
        chinese=_chinese_label(item)
        identity,label,rank=descriptor or ('field:'+key(chinese or item.get('label','')),None,95)
        old=groups.get(identity) if identity else None
        # Two independent authored text dictionaries may describe different scopes.
        if old and old.get('feature_id') and item.get('feature_id') and old['feature_id']!=item['feature_id']:
            old=None
        if old:
            old['selected']=bool(old.get('selected') or item.get('selected'))
            old['suggested_core']=bool(old.get('suggested_core') or item.get('suggested_core'))
            if chinese and old.get('label_zh') in _GENERIC_LABELS:old['label_zh']=chinese
            old['source_labels']=list(dict.fromkeys([*old.get('source_labels',[]),*item.get('source_labels',[])]))
            old['merged_ids']=list(dict.fromkeys([*old.get('merged_ids',[]),item['id'],*item.get('merged_ids',[])]))
            old['observations'].extend(item.get('observations') or [])
            continue
        if identity:groups[identity]=item
        item['label_zh']=chinese or label or ('自定义条件' if item.get('selected') else '其他商品参数')
        item['rule']='双方文字明确不一致才排除；缺失则继续后续层。一致仅通过本项筛选，不能据此确认同款。'
        item['display_priority']=rank
        items.append(item)
    for item in items:
        unique={}
        for proof in item.get('observations') or []:
            unique.setdefault((proof.get('asin'),proof.get('value'),bool(proof.get('image'))),proof)
        item['observations']=list(unique.values())
    # Authored dictionaries are extraction hints, not evidence that this batch
    # actually contains the parameter. Merge real observations before filtering.
    items=[item for item in items if item.get('kind')!='text' or item.get('origin')!='规则建议' or item.get('observations')]
    texts=[item for item in items if item.get('kind')=='text']
    texts.sort(key=lambda item:(not item.get('selected'),not item.get('suggested_core'),item.get('display_priority',95)))
    # Unknown untranslated fields stay in the original-parameter disclosure.
    # User selections are retained even when more than eight were selected.
    candidates=[item for item in texts if item.get('selected') or item.get('label_zh') not in _GENERIC_LABELS]
    core={item['id'] for item in candidates[:8]}
    for item in texts:item['display_group']='core' if item['id'] in core else 'more'
    result['items']=texts+[item for item in items if item.get('kind')!='text']
    result['display_version']=VERSION
    result['core_text_count']=sum(item['display_group']=='core' for item in texts)
    result['more_text_count']=len(texts)-result['core_text_count']
    result['raw_parameters']=list(raw.values())
    result['raw_parameter_count']=len(raw)
    return result
