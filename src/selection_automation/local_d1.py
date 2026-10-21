"""Native local d1 /v1/decide contract, without importing or loading its model."""
from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import re
from urllib.parse import urlsplit

from PIL import Image, ImageDraw, ImageFont, ImageOps

MODEL = 'LiquidAI/d1-3B'
SOURCE = 'D1 原生图片决策接口'
PATH = '/v1/decide'
MAX_QUESTIONS = 10
MAX_IMAGE_BASE64 = 8_000_000
MAX_IMAGE_PIXELS = 16_000_000
PRESENTATION = 'local-d1-amazon-left-temu-right-png-v1'


class LocalD1InputError(ValueError):
    def __init__(self, message):
        super().__init__(message)
        # The existing consumer persists only these safe diagnostic fields.
        self.body = {'error': {'code': 'LOCAL_D1_INPUT', 'type': 'local_input_error', 'message': message}}


def native_endpoint(provider):
    return (provider.get('mode') == 'local_model' and provider.get('protocol') == 'decisions'
            and urlsplit(provider.get('base_url', '')).path.rstrip('/') == PATH)


def _schema(document, schema):
    """Resolve only local OpenAPI references, with a finite depth."""
    for _ in range(5):
        if not isinstance(schema, dict):
            return {}
        reference = schema.get('$ref')
        if not reference:
            return schema
        if not isinstance(reference, str) or not reference.startswith('#/'):
            return {}
        schema = document
        for key in reference[2:].split('/'):
            schema = schema.get(key, {}) if isinstance(schema, dict) else {}
    return {}


def discover(base, get_json):
    """GET metadata only. A model name alone never advertises image support."""
    try:
        health = get_json(base, '/health')
        if not isinstance(health, dict) or health.get('ready') is not True or health.get('model') != MODEL:
            return None
        document = get_json(base, '/openapi.json')
        if not isinstance(document, dict) or document.get('info', {}).get('title') != 'Local d1-3B Decision API':
            return None
        operation = document.get('paths', {}).get(PATH, {}).get('post', {})
        body = _schema(document, operation.get('requestBody', {}))
        request = _schema(document, body.get('content', {}).get('application/json', {}).get('schema', {}))
        properties = request.get('properties', {})
        questions = _schema(document, properties.get('questions', {}))
        image = _schema(document, properties.get('image_base64', {}))
        image_types = image.get('anyOf', [image])
        if (request.get('type') != 'object' or 'questions' not in request.get('required', [])
                or 'state' not in properties or 'model' in properties or 'images' in properties
                or questions.get('type') != 'object' or questions.get('minProperties') != 1
                or questions.get('maxProperties') != MAX_QUESTIONS
                or not any(isinstance(item, dict) and item.get('type') == 'string'
                           and item.get('maxLength') == MAX_IMAGE_BASE64 for item in image_types)):
            return None
        return {'id': MODEL, 'name': MODEL, 'display_name': '本机 D1 原生图片决策接口',
                'vision': True, 'source': SOURCE, 'base_url': base.rstrip('/') + PATH,
                'protocol': 'decisions', 'size': 0, 'status': 'native_image_api_ready',
                'inference_verified': False,
                'capability_evidence': 'health_exact_model_and_openapi_image_base64',
                'limits': {'questions': MAX_QUESTIONS, 'image_base64': MAX_IMAGE_BASE64,
                           'image_pixels': MAX_IMAGE_PIXELS, 'text_tokens': 4096,
                           'token_preflight': 'unavailable_server_enforced'},
                'model_revision': health.get('revision')}
    except (OSError, ValueError, AttributeError, TypeError):
        return None


def question_batch_limit(category):
    from .match_criteria import selected
    per_pair = 1 + len(selected(category, 'visual'))
    if per_pair > MAX_QUESTIONS:
        raise LocalD1InputError(f'本机 D1 每次最多 {MAX_QUESTIONS} 题；单个商品对需要 {per_pair} 题。'
                                '已保留全部规则，未发送请求；请选择能承载完整规则的分析方案。')
    return MAX_QUESTIONS // per_pair


def _picture(value):
    if not isinstance(value, str) or not re.match(r'^data:image/[A-Za-z0-9.+-]+;base64,', value):
        raise LocalD1InputError('本机 D1 需要两张已读取的真实商品图片，不接受远程图片地址')
    try:
        data = base64.b64decode(value.split(',', 1)[1], validate=True)
        with Image.open(io.BytesIO(data)) as source:
            if source.width * source.height > MAX_IMAGE_PIXELS:
                raise LocalD1InputError('本机 D1 单张源图超过 1600 万像素；未发送请求')
            oriented = ImageOps.exif_transpose(source)
            rgba = oriented.convert('RGBA')
            picture = Image.new('RGB', rgba.size, 'white')
            picture.paste(rgba, mask=rgba.getchannel('A'))
        return picture, hashlib.sha256(data).hexdigest()
    except (OSError, ValueError, binascii.Error, Image.DecompressionBombError) as error:
        if isinstance(error, LocalD1InputError):
            raise
        raise LocalD1InputError('本机 D1 商品图片解码失败，未发送请求') from error


def compose_images(images):
    """Keep every source pixel; add explicit left/right labels without cropping."""
    if not isinstance(images, list) or len(images) != 2:
        raise LocalD1InputError('本机 D1 需要 Amazon 与 TEMU 两张源图')
    pictures = [_picture(value) for value in images]
    left, right = [item[0] for item in pictures]
    header, gap = 48, 16
    left_width, right_width = max(320, left.width), max(320, right.width)
    size = (left_width + right_width + gap, max(left.height, right.height) + header)
    if size[0] * size[1] > MAX_IMAGE_PIXELS:
        raise LocalD1InputError('双图完整合成后超过本机 D1 的 1600 万像素限制；未裁图、未发送请求')
    canvas = Image.new('RGB', size, 'white')
    boxes = [[0, header, left.width, header + left.height],
             [left_width + gap, header, left_width + gap + right.width, header + right.height]]
    for picture, box in zip((left, right), boxes):
        canvas.paste(picture, tuple(box[:2]))
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default(size=20)
    draw.text((8, 12), 'LEFT | Amazon | image 1', fill='black', font=font)
    draw.text((left_width + gap + 8, 12), 'RIGHT | TEMU | image 2', fill='black', font=font)
    output = io.BytesIO()
    canvas.save(output, format='PNG')
    data = output.getvalue()
    encoded = base64.b64encode(data).decode('ascii')
    if len(encoded) > MAX_IMAGE_BASE64:
        raise LocalD1InputError('双图 PNG 超过本机 D1 的 image_base64 长度限制；未压缩删减、未发送请求')
    metadata = {'presentation': PRESENTATION, 'mime_type': 'image/png', 'size': list(size),
                'sha256': hashlib.sha256(data).hexdigest(),
                'sources': [{'role': role, 'encoded_source_sha256': item[1], 'box': box}
                            for role, item, box in zip(('amazon', 'temu'), pictures, boxes)]}
    return encoded, metadata


def prepare_payload(payload, provider):
    if provider.get('model') != MODEL:
        raise LocalD1InputError('本机 D1 原生接口模型身份必须为 LiquidAI/d1-3B；未发送请求')
    questions = payload['questions']
    if not 1 <= len(questions) <= MAX_QUESTIONS:
        raise LocalD1InputError(f'本机 D1 每次最多 {MAX_QUESTIONS} 题，本次需要 {len(questions)} 题；'
                                '全部规则已保留，未发送请求')
    for question in questions.values():
        if (not isinstance(question, dict) or question.get('type') != 'choice'
                or not isinstance(question.get('criteria'), dict) or not question['criteria']):
            raise LocalD1InputError('本机 D1 需要完整 choice 问题与判断条件；未发送请求')
    image, metadata = compose_images(payload['images'])
    state = {**payload['state'], 'image_layout':
             'One PNG contains both complete source images. LEFT / image 1 = Amazon reference; '
             'RIGHT / image 2 = TEMU candidate (or its numbered grid). Compare the two sides; '
             'Cxx candidate labels, when present, apply only to the RIGHT TEMU pane.'}
    # This service exposes no tokenizer endpoint. Do not truncate rules or call
    # character counts tokens; its actual tokenizer enforces the 4096-token cap.
    return {'state': state, 'questions': questions, 'image_base64': image}, metadata


def prepared_image(data):
    """Recover the exact transmitted PNG for an operator's acceptance evidence."""
    payload = json.loads(data)
    image = base64.b64decode(payload['image_base64'], validate=True)
    with Image.open(io.BytesIO(image)) as picture:
        if picture.format != 'PNG' or picture.width * picture.height > MAX_IMAGE_PIXELS:
            raise LocalD1InputError('已准备的本机 D1 PNG 证据无效')
    return image


def rejection_body(response):
    """Project FastAPI detail without its potentially huge/sensitive input field."""
    try:
        detail = response.json().get('detail')
    except (ValueError, AttributeError, TypeError):
        detail = None
    if isinstance(detail, list):
        detail = '; '.join(str(item.get('msg', '')) for item in detail[:5] if isinstance(item, dict))
    if not isinstance(detail, str) or not detail.strip():
        detail = '本机 D1 拒绝了请求，请检查服务日志中的输入限制'
    detail = re.sub(r'data:image/\S+', '[IMAGE_OMITTED]', detail)
    detail = re.sub(r'[A-Za-z0-9+/=_-]{80,}', '[LONG_VALUE_OMITTED]', detail)
    detail = re.sub(r'(?i)Bearer\s+\S+', 'Bearer [REDACTED]', detail)
    if response.status_code == 422 and '4096' in detail and 'token' in detail.lower():
        detail += '；服务使用真实 tokenizer 判定超限，规则未删减，本次不会自动重试'
    return {'error': {'code': f'LOCAL_D1_HTTP_{response.status_code}', 'type': 'local_d1_rejection',
                      'message': detail[:600]}}
