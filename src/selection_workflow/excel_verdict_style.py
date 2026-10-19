"""Consistent final-verdict row colors; styling never changes a conclusion."""
COLORS = {'different': '#FCE4D6', 'review': '#FFF2CC', 'same': '#E2F0D9', 'similar':'#DDEBF7', 'unanalysed': '#E7E6E6'}
LEGEND = '颜色：不同款浅红；待复核或待补充证据浅黄；同款浅绿；类似款浅蓝；未分析或未入围灰色。'


def verdict_kind(value):
    value = str(value or '').replace('（Codex看图复核）', '')
    if '类似款' in value or '相似款' in value or value=='SIMILAR':return 'similar'
    if '不同款' in value or value in {'DIFFERENT', 'NOT_SAME'}:
        return 'different'
    if any(word in value for word in ('复核', '补充', '未知', '不确定', '未确认')) or value == 'UNKNOWN':
        return 'review'
    if '同款' in value or value in {'SAME', 'CONFIRMED_SAME'}:
        return 'same'
    return 'unanalysed'


def verdict_formats(book, *, font_name='Arial', font_size=10):
    rows = {}
    for kind, color in COLORS.items():
        common = {'font_name': font_name, 'font_size': font_size, 'bg_color': color, 'valign': 'top'}
        rows[kind] = {
            'text': book.add_format({**common, 'text_wrap': True}),
            'center': book.add_format({**common, 'text_wrap': True, 'align': 'center'}),
            'number': book.add_format({**common, 'num_format': '0.00', 'align': 'center'}),
            'link': book.add_format({**common, 'font_color': 'blue', 'underline': 1, 'align': 'center'}),
        }
    return rows
