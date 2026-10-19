"""One Amazon reference and up to twelve numbered TEMU tiles, with provenance."""
from functools import lru_cache
import hashlib
import io
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

BATCH_SIZE = 12
PRESENTATION = 'shared-amazon-numbered-temu-grid-v1'


@lru_cache(maxsize=512)
def _digest(path, modified, size):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def image_digest(path):
    path=Path(path).resolve();stat=path.stat()
    return _digest(str(path),stat.st_mtime_ns,stat.st_size)


def amazon_key(row):
    path=row.get('amazon_image_path')
    try:picture=image_digest(path)
    except (OSError,TypeError):picture=str(path)
    return row['asin'],row.get('amazon_fact_version'),picture


def groups(rows, size=BATCH_SIZE):
    buckets={}
    for row in rows:buckets.setdefault(amazon_key(row),[]).append(row)
    return [values[offset:offset+size] for values in buckets.values()
            for offset in range(0,len(values),size)]


def render_grid(rows, cell=800):
    from selection_workflow.api_image_input import ImageInputError, validate_api_image
    if not rows or len(rows)>BATCH_SIZE:raise ValueError('视觉分组必须包含 1–12 个候选')
    if len({amazon_key(row) for row in rows})!=1:raise ValueError('同一组必须使用同一个 Amazon 商品及基准图')
    columns=min(3,len(rows));label_height=36
    canvas=Image.new('RGB',(columns*cell,((len(rows)+columns-1)//columns)*(cell+label_height)),'white')
    draw=ImageDraw.Draw(canvas);font=ImageFont.load_default(size=20);mapping=[]
    for index,row in enumerate(rows):
        x=(index%columns)*cell;y=(index//columns)*(cell+label_height)
        path=Path(row['temu_image_path']).resolve()
        try:
            with Image.open(path) as source:
                validate_api_image(source)
                original=ImageOps.exif_transpose(source).convert('RGB')
                picture=ImageOps.contain(original,(cell-12,cell-12),Image.Resampling.LANCZOS)
                canvas.paste(picture,(x+(cell-picture.width)//2,y+label_height+(cell-picture.height)//2))
        except ImageInputError:
            raise
        except (OSError, ValueError) as error:
            raise ImageInputError('IMAGE_DECODE_FAILED') from error
        label=f'C{index:02}'
        draw.text((x+8,y+7),label+' | '+str(row['goods_id']),fill='black',font=font)
        mapping.append({'id':label,'asin':row['asin'],'goods_id':row['goods_id'],
            'vision_revision_key':row.get('vision_revision_key'),'path':str(path),'sha256':image_digest(path),
            'box':[x,y,x+cell,y+label_height+cell]})
    stream=io.BytesIO();canvas.save(stream,format='JPEG',quality=95,subsampling=0)
    return stream.getvalue(),mapping


def save_assets(job,rows):
    job=Path(job);folder=job/'attachments';folder.mkdir(exist_ok=True)
    data,mapping=render_grid(rows);grid=folder/'temu-numbered-grid.jpg';grid.write_bytes(data)
    amazon=Path(rows[0]['amazon_image_path']).resolve()
    return {'presentation':PRESENTATION,'attachments':[
        {'role':'amazon','path':str(amazon),'sha256':image_digest(amazon)},
        {'role':'temu_grid','path':str(grid.resolve()),'sha256':hashlib.sha256(data).hexdigest()}],
        'grid_candidates':mapping}


def check_assets(inputs,pair):
    if inputs.get('presentation')!=PRESENTATION:return
    for item in inputs['attachments']:
        if image_digest(item['path'])!=item['sha256']:raise ValueError('视觉拼图或基准图发生变化')
    tile=next((item for item in inputs['grid_candidates'] if item['asin']==pair['asin']
               and item['goods_id']==pair['goods_id'] and item['vision_revision_key']==pair.get('vision_revision_key')),None)
    if tile is None or image_digest(pair['temu_image_path'])!=tile['sha256']:
        raise ValueError('TEMU 编号拼图与候选原图绑定不一致')


def comparison_input(rows):
    from .visual_text_evidence import product_evidence
    return {'presentation':PRESENTATION,'amazon':product_evidence(rows[0],'amazon') or {'product_id':rows[0]['asin']},
        'candidates':[{'id':f'C{index:02}','asin':row['asin'],'goods_id':row['goods_id'],
            'vision_revision_key':row['vision_revision_key'],'record':product_evidence(row,'temu')}
            for index,row in enumerate(rows)],
        'image_order':'图片1为唯一 Amazon 基准原图；图片2为 TEMU 编号拼图。C00 起按行从左至右，逐个独立与 Amazon 比较。'}


@lru_cache(maxsize=32)
def _cached_url(path,modified,size,encoder):
    return encoder(path)


def data_url(path,encoder=None):
    if encoder is None:
        from selection_workflow.bath_towel_deepseek import _data_url
        encoder=_data_url
    path=Path(path).resolve()
    try:stat=path.stat()
    except OSError:return encoder(str(path))
    return _cached_url(str(path),stat.st_mtime_ns,stat.st_size,encoder)


def grid_data_url(rows):
    import base64
    data,mapping=render_grid(rows)
    return 'data:image/jpeg;base64,'+base64.b64encode(data).decode('ascii'),mapping
