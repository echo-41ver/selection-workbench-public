"""A small set of comparison dimensions, independent of product-specific values."""
from copy import deepcopy
from .match_criteria import preserve,merge_proposals

VERSION='all-product-visual-auto-v1'
BATCH_SIZE=8
AXES={
    'shape':('外形轮廓','比较主体外形、轮廓和比例',True),
    'color':('颜色','比较主体及对应区域的颜色；忽略光照和白平衡差异',False),
    'pattern_content':('图案内容','比较印花的图形、文字和构图内容',True),
    'pattern_position':('图案位置','比较图案在商品上的位置、方向和排列',True),
    'part_shape':('部件造型','比较对应部件的实际形状和连接构造，不是仅检查是否都有该部件',True),
    'part_layout':('部件位置','比较对应部件的位置、排列和相对比例',True),
    'texture':('表面纹理','比较可见纹理和表面处理，不凭外观猜材质成分',False),
    'contents':('构成与数量','比较实际出售主体、配套部件和数量，区分展示道具',True),
}


def option(axis):
    label,rule,default=AXES[axis]
    return {'id':'visual_'+axis,'label':label,'kind':'visual','dimension':axis,
            'rule':rule+'；勾选才要求一致，未勾选的差异不作为否决条件。',
            'selected':False,'feature_id':None,'source_labels':[],
            'observations':[],'comparison':'exact','origin':'可选外观比较项'}


def start(catalog):
    result=deepcopy(catalog)
    result['items']=[item for item in result['items'] if item['kind']=='text']
    result['items'].extend(option(axis) for axis in ('shape','color'))
    result['visual_version']=VERSION
    result['visual_mode']='auto'
    return preserve(result,catalog if catalog.get('visual_version')==VERSION else None)


def merge(catalog,snapshot,batch,proposal):
    if not isinstance(proposal,dict) or not isinstance(proposal.get('items'),list):
        raise ValueError('图片条件返回格式无效')
    expected={row['asin'] for row in batch}
    if set(proposal.get('reviewed_asins') or [])!=expected:
        raise ValueError('模型未完整确认本批图片覆盖范围')
    text={**proposal,'items':[row for row in proposal['items'] if isinstance(row,dict) and row.get('kind')=='text']}
    result=merge_proposals(catalog,snapshot,text)
    items={item['id']:item for item in result['items']}
    for row in proposal['items']:
        if not isinstance(row,dict) or row.get('kind')!='visual' or row.get('dimension') not in AXES:continue
        axis=row['dimension'];proofs=[]
        for proof in row.get('evidence') or []:
            if not isinstance(proof,dict) or proof.get('asin') not in expected or proof.get('image') is not True:continue
            value=proof.get('quote')
            if not isinstance(value,str) or not value.strip():continue
            proofs.append({'asin':proof['asin'],'value':value.strip()[:400],'image':True,'source_label':'实际主图'})
        if not proofs:continue
        item=items.setdefault('visual_'+axis,option(axis))
        item['origin']='全部商品主图提取后按属性合并'
        for proof in proofs:
            if proof not in item['observations']:item['observations'].append(proof)
    result['items']=list(items.values())
    result['automatic_focus']=[item['label'] for item in result['items'] if item['kind']=='visual' and item.get('observations')]
    return preserve(result,catalog)


def prompt(category,snapshot,catalog,batch):
    import json
    from .match_criteria import proposal_prompt
    scoped=deepcopy(catalog);scoped['image_sources']=batch
    asins={row['asin'] for row in batch}
    scoped_snapshot={'records':[row for row in snapshot.get('records',[]) if row.get('asin') in asins]}
    return (proposal_prompt(category,scoped_snapshot,scoped)+
        '\n本次必须逐张核对全部附件；额外返回reviewed_asins数组，列出已核对的所有附件ASIN。'
        '图片选项是比较维度，不是部件存在要求，也不是某个商品的特定值。'
        '例如浴帘的颜色、图案内容、图案位置分别各一项，不按商品、颜色值或花纹值拆项。'
        'visual条目必须增加dimension字段，只能选下面适用的维度；同维度的部位及商品取并集。'
        'evidence.quote简短记录该商品图片实际可见的特征（中文），不要抄“应当有什么”的规则；'
        '不要把边框颜色允许变化等旧规则作为事实。只输出本批实际可见的维度，不补猜隐藏部位。'
        '\n可用维度：'+json.dumps({key:value[0] for key,value in AXES.items()},ensure_ascii=False))
