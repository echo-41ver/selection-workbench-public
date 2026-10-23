"""Selection summary and matching detail, with clickable embedded photographs."""
from __future__ import annotations
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from PIL import Image
from selection_automation.selection_delivery import build_delivery,conclusion_label,short_reason,collected_parameters,display_parameter
from selection_automation.commercial import jsonl
from selection_automation.model_labels import model_label
from selection_workflow.excel_images import cached_thumbnail


def export(root,category,report):
    from export_category_workbooks import _formats,_publish_formatted,_write_value
    data=build_delivery(root,category,report)
    name=data['category_name'];base=Path(root)/'runs'/category['category_key']
    destination=base/'excel'/f'{name}_选品结果.xlsx'
    note=(f"{data['collection']['label']}。选品结论仅保留本次采集完成、采样核验完成且同款＋类似款严格小于 {data['limit']} 的 Amazon。"
          '采集分析进度保留阶段性数据；零计数不表示已核验或零竞争。明细保留已完成的同款和类似款判断；待复核依据保留无法判断的实际配对，不计入选品结论。点击图片可打开原图。')
    parameter_headers=['当前价(USD)','最低价(USD)','最高价(USD)','价格带口径','评分','评论数','页面销量','销量口径','真实上架时间','上架时间来源','采集时间','采集来源']
    amazon_raw={i['asin']:i['raw'] for i in data['progress']}
    amazon_raw.update({p['asin']:p['amazon_raw'] for p in data['pairs']})
    temu_raw={p['goods_id']:p['temu_raw'] for p in data['pairs'] if p['temu_raw'] is not None}
    standard={'评分','评论数','页面销量','近一月页面销量','销量口径','采集时间','采集来源','当前价格(USD)','上架时间','上架时间来源'}
    extras={side:list(dict.fromkeys(k for r in records.values() for k in collected_parameters(r) if k not in standard)) for side,records in [('Amazon',amazon_raw),('TEMU',temu_raw)]}
    summary_headers=['Amazon ASIN','Amazon标题','Amazon图片','同款数','类似款数','不同款数','无法判断数','未分析数','同款+类似款数量','选品状态','判断依据']+['Amazon'+h for h in parameter_headers]+['Amazon'+h for h in extras['Amazon']]+['Amazon商品链接']
    progress_headers=['Amazon ASIN','Amazon标题','Amazon图片','采集状态','分析状态','状态说明','同款数','类似款数','不同款数','无法判断数','未分析数','同款+类似款数量','Amazon商品链接']
    prefix=['Amazon ASIN','Amazon标题','Amazon图片','TEMU图片','同款+类似款数量','Amazon商品链接','TEMU商品链接']
    pair_headers=prefix+['TEMU商品ID','TEMU标题','最终关系','判断依据','分析模型']+[side+h for side in ('Amazon','TEMU') for h in parameter_headers]+[side+h for side in ('Amazon','TEMU') for h in extras[side]]+['视觉证据','同款决策概率','辅助评分']
    local_images={}
    for row in jsonl(base/f"stage123/export_frozen/layer3_candidates_{category['category_key']}.jsonl"):
        for side,id_key,wanted in [('amazon','asin',amazon_raw),('temu','goods_id',temu_raw)]:
            identity=str(row.get(id_key) or '');path=row.get(side+'_image_path')
            if identity in wanted and path and Path(path).is_file():local_images.setdefault((side,identity),path)
    assets={('amazon',i['asin']):(i['amazon'],i['raw']) for i in data['progress']}
    assets.update({('amazon',p['asin']):(p['amazon'],p['amazon_raw']) for p in data['pairs']})
    assets.update({('temu',p['goods_id']):(p['temu'],p['temu_raw']) for p in data['pairs'] if p['temu_raw'] is not None})
    def thumbnail(entry):
        identity,(card,raw)=entry
        return identity,cached_thumbnail({'asin':identity[1],'main_image_url':card['image'],'local_image_path':local_images.get(identity) or raw.get('local_image_path')},base/'excel/embedded_image_cache')
    with ThreadPoolExecutor(max_workers=4) as pool:images=dict(pool.map(thumbnail,assets.items()))
    def fields(card):
        if card is None:return [None]*len(parameter_headers)
        p,s=card['price'],card['sales']
        return [display_parameter(value) for value in [p['observed_price'],p['min'],p['max'],p['basis'],card['rating'],card['review_count'],s['text'],s['basis'],card['listed_at'],card['listed_at_source'],card['observed_at'],card['source']]]
    def extra_values(side,raw):
        values=collected_parameters(raw)
        return [values.get(k) for k in extras[side]]
    def build(book):
        f=_formats(book)
        def sheet(title,headers):
            ws=book.add_worksheet(title);ws.hide_gridlines(2)
            ws.merge_range(0,0,0,len(headers)-1,name+'｜'+title,f['title']);ws.merge_range(1,0,1,len(headers)-1,note,f['note'])
            ws.set_row(0,28);ws.set_row(1,38);ws.set_row(3,38);ws.write_row(3,0,headers,f['header']);ws.freeze_panes(4,3)
            for col,h in enumerate(headers):
                width=44 if '标题' in h else 48 if any(k in h for k in ('依据','规格','证据','描述')) else 22 if '图片' in h else 18
                ws.set_column(col,col,width,None,{'hidden':True} if h in ('视觉证据','辅助评分','同款决策概率') else {})
            return ws
        def emit(ws,index,headers,values):
            for col,(h,v) in enumerate(zip(headers,values)):
                if isinstance(v,str) and len(v)>32700:v=v[:32680]+'…完整内容见商品完整字段'
                _write_value(ws,index,col,v if v is not None else '未取得',f['money'] if '(USD)' in h else f['text'],f,h,native_links=True)
            ws.set_row(index,100)
        def picture(ws,index,col,side,identity):
            path=images.get((side,identity));asset=assets.get((side,identity))
            if not path:ws.write(index,col,'图片未取得',f['text']);return
            with Image.open(path) as image:scale=min(120/image.width,120/image.height)
            opts={'x_scale':scale,'y_scale':scale,'x_offset':4,'y_offset':4,'object_position':1}
            url=asset[0].get('image') if asset else None
            if url and str(url).startswith(('https://','http://')):opts.update(url=url,tip='打开原图')
            ws.insert_image(index,col,str(path),opts)
        summary=sheet('选品结论',summary_headers)
        for index,item in enumerate(data['products'],4):
            c,m=item['amazon'],item['match']
            emit(summary,index,summary_headers,[item['asin'],c['title'],'',item['confirmed_count'],item['similar_count'],m.get('different',0),m.get('possible',0),m.get('unanalysed',m.get('pending',0)),item['same_count'],item['analysis_status'],item['advice']['reason'],*fields(c),*extra_values('Amazon',item['raw']),c['url']])
            picture(summary,index,2,'amazon',item['asin'])
        summary.autofilter(3,0,max(3,len(data['products'])+3),len(summary_headers)-1)
        progress=sheet('采集分析进度',progress_headers)
        for index,item in enumerate(data['progress'],4):
            c,m=item['amazon'],item['match']
            emit(progress,index,progress_headers,[item['asin'],c['title'],'',item['collection_status'],item['analysis_status'],item['status_reason'],item['confirmed_count'],item['similar_count'],m.get('different',0),m.get('possible',0),m.get('unanalysed',m.get('pending',0)),item['same_count'],c['url']])
            picture(progress,index,2,'amazon',item['asin'])
        progress.autofilter(3,0,max(3,len(data['progress'])+3),len(progress_headers)-1)
        detail=sheet('同款商品明细',pair_headers)
        for index,item in enumerate(data['pairs'],4):
            e=item['evidence'];a,t=item['amazon'],item['temu']
            emit(detail,index,pair_headers,[item['asin'],a['title'],'','',item['same_count'],a['url'],t['url'] if t else None,item['goods_id'],t['title'] if t else None,conclusion_label(item['relation']),short_reason(e.get('reason','已通过当前版本最终关系判断')),model_label(e.get('model'),root),*fields(a),*fields(t),*extra_values('Amazon',item['amazon_raw']),*extra_values('TEMU',item['temu_raw']),e.get('evidence'),e.get('probability'),e.get('score')])
            picture(detail,index,2,'amazon',item['asin']);picture(detail,index,3,'temu',item['goods_id'])
        detail.autofilter(3,0,max(3,len(data['pairs'])+3),len(pair_headers)-1)
        reviews=(report or {}).get('review_pairs',[])
        if reviews:
            review_headers=['Amazon ASIN','TEMU商品ID','最终判断','判断依据','视觉证据','Amazon商品链接','TEMU商品链接','Amazon标题','TEMU标题','分析模型']
            review_sheet=sheet('待复核依据',review_headers)
            for index,item in enumerate(reviews,4):
                values=[item.get(k) for k in ('asin','goods_id','state','reason','evidence','amazon_url','temu_url','amazon_title','temu_title')]+[model_label(item.get('model'),root)]
                emit(review_sheet,index,review_headers,values)
                # Cache the original URL as the cell value, so the read-only web view
                # can use this same published workbook without reading live results.
                for col in (5,6):
                    url=values[col]
                    if isinstance(url,str) and url.startswith('https://'):
                        review_sheet.write_url(index,col,url,f['link'],string=url)
            review_sheet.autofilter(3,0,max(3,len(reviews)+3),len(review_headers)-1)
        headers=['平台','商品ID','采集字段','已采集内容','内容分段','商品来源链接'];extra=sheet('商品完整字段',headers);extra.hide();count=0
        for side,records in [('Amazon',amazon_raw),('TEMU',temu_raw)]:
            for identity,raw in records.items():
                for k,v in collected_parameters(raw,include_core=True).items():
                    text=str(v) if v is not None else '未取得'
                    chunks=[text[i:i+32000] for i in range(0,len(text),32000)] or ['']
                    for part,chunk in enumerate(chunks,1):emit(extra,count+4,headers,[side,identity,k,chunk,f'{part}/{len(chunks)}',raw.get('product_url')]);count+=1
        extra.set_column(3,3,90);extra.autofilter(3,0,max(3,count+3),len(headers)-1)
        return '选品结论',4,len(data['products'])
    delivered=_publish_formatted(destination,build)
    return {**delivered,'交付格式':'精简选品结果','入选Amazon数':len(data['products']),'采集状态':data['collection']['label'],'进度Amazon数':len(data['progress']),'确认同款对数':sum(p['relation']=='确认同款' for p in data['pairs']),'类似款对数':sum(p['relation']=='相似款' for p in data['pairs']),'同款数门槛':data['limit']}
