"""Complete offline preparation child outputs; never fetch sites or call a model."""
import json
from pathlib import Path


def completed_amazon_snapshot(category, title='Cordless power drill kit'):
    image='https://m.media-amazon.com/images/I/offline-fixture.jpg'
    record={'asin':'B000000001','rank':1,'title':title,
            'product_url':'https://www.amazon.com/dp/B000000001',
            'main_image_url':image,'specifications':[{'label':'Construction','value':'Fixture body'}],
            'detail_status':'详情页已采集','image_status':'详情页主图',
            'detail_quality':{'schema_version':'amazon-detail-quality-v1','title_verified':True,
                'missing_fields':[],'image':{'verified':True,'decoded':True,'url':image,
                    'http_status':200,'mime':'image/jpeg','width':900,'height':900}}}
    return {'schema_version':'amazon-bestsellers-frontend-v1','status':'COMPLETE',
            'listing_complete':True,'details_complete':True,'complete':True,
            'records':[record],'record_count':1,'target_count':1,
            'source_url':category['amazon']['bestsellers_url'],'category_key':category['category_key']}


def extract_complete_visual(arguments):
    """Exercise the real image merge and coverage contract using an injected reply."""
    from extract_match_criteria import extract
    category_path=Path(arguments[arguments.index('--category')+1])
    amazon_path=Path(arguments[arguments.index('--amazon')+1])
    category=json.loads(category_path.read_text('utf-8'))
    snapshot=json.loads(amazon_path.read_text('utf-8'))
    def answer(question,**kwargs):
        asins=json.loads(question.split('附件图片依次属于：',1)[1].split('\n',1)[0])
        assert kwargs['images']==[row['main_image_url'] for row in snapshot['records']]
        return {'reviewed_asins':asins,'items':[]}
    value=extract(category,snapshot,answer)
    category_path.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8')
    return value
