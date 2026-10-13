"""Commercial result sheets shared by all four user-facing workbooks."""
def add_report(book, report):
    if not report: return
    head = book.add_format({'bold': True, 'font_name': 'Microsoft YaHei', 'font_color': 'white',
                            'bg_color': '#1F4E78', 'text_wrap': True, 'valign': 'vcenter'})
    text = book.add_format({'font_name': 'Microsoft YaHei', 'text_wrap': True, 'valign': 'top'})
    styles = {state: book.add_format({'font_name': 'Microsoft YaHei', 'text_wrap': True,
                                    'valign': 'top', 'bg_color': color})
              for state, color in [('UNKNOWN', '#FFF2CC'), ('FAIL', '#FCE4D6'),
                                   ('SAMPLE_PASS', '#E2F0D9'), ('PASS', '#E2F0D9')]}
    ws = book.add_worksheet('商业选品'); ws.hide_gridlines(2)
    headers = ['ASIN', 'Amazon标题', '榜单排名', '已确认同款', '疑似待复核', '尚未分析', '向量未入围',
               '同款数判断', '同款数判断依据', '新品增长判断', '新品增长依据', '头部集中度判断',
               '头部集中度依据', '正向采样信号数', '待补证据项数', '建议', '利润状态']
    ws.write_row(0, 0, headers, head); ws.set_row(0, 34); ws.freeze_panes(1, 1)
    market = report['market']; growth = market['growth']; concentration = market['concentration']
    for index, row in enumerate(report['products'], 1):
        match = row['match']
        values = [row['asin'], row.get('title'), row.get('rank'), match['confirmed'], match['possible'], match['pending'],
                  match['screened_out'], match['label'], match['reason'], growth['label'], growth['reason'],
                  concentration['label'], concentration['reason'], row['signals'], row['unknown'], row['priority'], row['profitability']]
        for col, value in enumerate(values):
            ws.write(index, col, value, styles[(match if col < 9 else growth if col < 11 else concentration)['state']] if col in (7, 9, 11) else text)
        ws.set_row(index, 64)
    for col in range(len(headers)): ws.set_column(col, col, 42 if col in (1, 8, 10, 12, 16) else 22)
    ws.autofilter(0, 0, max(1, len(report['products'])), len(headers)-1)
    proof = book.add_worksheet('市场证据'); proof.hide_gridlines(2)
    proof.set_column('A:A', 32); proof.set_column('B:B', 100)
    proof.write_row(0, 0, ['指标', '实际口径与数据'], head)
    policy = report['policy']
    rows = [('报告时间', report['as_of']), ('数据范围', report['scope']),
            ('同款门槛', f"已确认同款 ≤ {policy['same_limit']}；疑似与未分析分开"),
            ('新品定义', f"有来源的上架日期距今不超过 {policy['new_days']} 天；首次观测不可替代"),
            ('增长门槛', f"增长中位数 > {policy['growth_min_percent']}%；周期：{policy['growth_period']}"),
            ('类目采样商品数', market['market_products']),
            ('类目采样关键词', market.get('primary_category_query')),
            ('所有关键词前端商品数（不全用于类目统计）', market.get('all_frontend_products', market['market_products'])),
            ('有上架证据的新品数', market['dated_new_products']),
            ('累计有效观测日数', market['history_days']), ('历史观测条数', market['observations']),
            ('最早观测', market['first_observation']), ('最近观测', market['last_observation']),
            ('集中度样本销量覆盖率', concentration['coverage']),
            ('头部占比', concentration['value_percent'] if concentration['value_percent'] is not None else '证据不足'),
            ('已知样本头部占比（不足覆盖时仅供参考）', concentration.get('known_sample_percent')),
            ('集中度口径', f"近30天销量前 {policy['top_n']} 名占采样销量 < {policy['top_share_max_percent']}%"),
            ('集中度说明', concentration['reason'])]
    for name, period in growth['periods'].items():
        rows += [(f"{'周' if name=='week' else '月'}环比增长中位数(%)", period['value'] if period['value'] is not None else '证据不足'),
                 (f"{'周' if name=='week' else '月'}环比有效商品数", period['valid_products']),
                 (f"{'周' if name=='week' else '月'}环比有效覆盖率", period['coverage'])]
    if market.get('frontend_sort'):
        rows += [('TEMU网页销量排序', market['frontend_sort']['reason'])]
        for check in market['frontend_sort']['checks']:
            rows += [('排序检查', f"{check['query']} · {check['state']} · {check.get('label') or '未确认'} · {check.get('observed_at') or '无确认时间'}")]
    provider=market.get('provider')
    if provider:
        rows += [('市场数据来源','极鲸云；不与真实前端采集混写'),('极鲸云查询时间',provider['queried_at']),
                 ('极鲸云商品样本数',provider['products']),('极鲸云近期上架商品数',provider['new_products']),
                 ('极鲸云实际查询页数',provider['pages']),('极鲸云收录分页已完整',provider['complete'])]
        for category in provider['categories']:
            rows += [('数据提供方类目',category.get('path') or category['name']),('类目来源链接',category.get('link')),
                     ('已取/命中',f"{category['retrieved']}/{category['expected']}")]
        evidence=book.add_worksheet('极鲸云商品证据');evidence.hide_gridlines(2)
        evidence.write_row(0,0,['商品ID','商品名称（来源链接）','上架时间','数据更新时间','累计销量（提供方）',
                                '周销量','月销量','周销量环比（%）','月销量环比（%）','来源类目ID'],head)
        evidence.set_column('A:A',24);evidence.set_column('B:B',60);evidence.set_column('C:D',32);evidence.set_column('E:J',22)
        entries=provider.get('evidence_rows',[])
        for index,row in enumerate(entries,1):
            name=row.get('goodsNameCn') or row.get('goodsName') or row['goodsId']
            evidence.write(index,0,row['goodsId'],text)
            url=row.get('linkUrl')
            if isinstance(url,str) and url.startswith('https://www.geekbi.com/'):
                evidence.write_url(index,1,url,text,string=name)
            else:evidence.write_string(index,1,name,text)
            evidence.write_row(index,2,[row.get('onSaleTime'),row.get('updateTime'),row.get('sold'),row.get('weekSold'),
                row.get('monthSold'),row['weekSoldRate']*100 if type(row.get('weekSoldRate')) in (float,int) else None,
                row['monthSoldRate']*100 if type(row.get('monthSoldRate')) in (float,int) else None,
                ','.join(map(str,row.get('catIds') or []))],text)
        evidence.freeze_panes(1,2);evidence.autofilter(0,0,max(1,len(entries)),9)
    for index, (key, value) in enumerate(rows, 1):
        proof.write(index, 0, key, text); proof.write(index, 1, value, text)
    proof.freeze_panes(1, 0)
