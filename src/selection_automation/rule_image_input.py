"""Verified Amazon original bytes for rule-model attachments; no provider fetch."""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
from urllib.parse import urljoin, urlsplit
import uuid

from PIL import Image
from selection_workflow.api_image_input import validate_api_image
from selection_workflow.image_transport import ImageTransport

HOSTS=frozenset({'m.media-amazon.com','images-na.ssl-images-amazon.com','images-eu.ssl-images-amazon.com'})
MAX_BYTES=16*1024*1024


def allowed_url(url):
    try:
        parsed=urlsplit(url)
        return (parsed.scheme=='https' and parsed.hostname in HOSTS and not parsed.username
                and not parsed.password and parsed.port is None and not parsed.fragment)
    except (ValueError,TypeError):return False


def _decode(data):
    if not 100<=len(data)<=MAX_BYTES:raise ValueError('IMAGE_SIZE_INVALID')
    with Image.open(io.BytesIO(data)) as image:
        validate_api_image(image)
        return Image.MIME[image.format],image.size


def _download(transport,url):
    current=url
    for _ in range(4):
        if not allowed_url(current):raise ValueError('IMAGE_URL_NOT_ALLOWED')
        with transport._client(None).stream('GET',current,follow_redirects=False) as reply:
            if reply.status_code in {301,302,303,307,308}:
                current=urljoin(current,reply.headers.get('location',''))
                if not allowed_url(current):raise ValueError('IMAGE_REDIRECT_NOT_ALLOWED')
                continue
            reply.raise_for_status()
            if reply.status_code!=200:raise ValueError('IMAGE_HTTP_STATUS_INVALID')
            length=reply.headers.get('content-length')
            if length and int(length)>MAX_BYTES:raise ValueError('IMAGE_SIZE_INVALID')
            data=bytearray()
            for chunk in reply.iter_bytes():
                data.extend(chunk)
                if len(data)>MAX_BYTES:raise ValueError('IMAGE_SIZE_INVALID')
            return bytes(data),current
    raise ValueError('IMAGE_REDIRECT_LIMIT')


def _atomic(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.parent/('.'+uuid.uuid4().hex+'.tmp')
    try:
        with temporary.open('xb') as stream:stream.write(data)
        os.replace(temporary,path)
    finally:temporary.unlink(missing_ok=True)


def prepare_images(root,urls):
    if not isinstance(urls,(list,tuple)) or not 1<=len(urls)<=8:raise ValueError('IMAGE_COUNT_INVALID')
    if not all(isinstance(url,str) and allowed_url(url) for url in urls):raise ValueError('IMAGE_URL_NOT_ALLOWED')
    root=Path(root).resolve();cache=root/'runtime/model-image-cache'
    if not cache.resolve().is_relative_to(root):raise ValueError('IMAGE_CACHE_OUTSIDE_ROOT')
    wire=[];bindings=[];transport=ImageTransport(timeout=30)
    try:
        for url in urls:
            metadata=cache/(hashlib.sha256(url.encode()).hexdigest()+'.json')
            data=None;final_url=url
            try:
                saved=json.loads(metadata.read_text('utf-8'))
                digest=saved['content_sha256']
                if (saved['source_url']!=url or not isinstance(digest,str) or len(digest)!=64
                        or any(c not in '0123456789abcdef' for c in digest)
                        or not allowed_url(saved['download_url'])):raise ValueError('IMAGE_CACHE_BINDING_INVALID')
                image_path=cache/(digest+'.img')
                if not image_path.resolve().is_relative_to(cache.resolve()):raise ValueError('IMAGE_CACHE_OUTSIDE_ROOT')
                data=image_path.read_bytes()
                if hashlib.sha256(data).hexdigest()!=digest:raise ValueError('IMAGE_CACHE_CONTENT_CHANGED')
                _decode(data);final_url=saved['download_url']
            except (OSError,ValueError,KeyError,TypeError,SyntaxError):data=None
            if data is None:
                data,final_url=_download(transport,url)
                mime,size=_decode(data)
                digest=hashlib.sha256(data).hexdigest()
                _atomic(cache/(digest+'.img'),data)
                _atomic(metadata,json.dumps({'source_url':url,'download_url':final_url,
                    'content_sha256':digest},ensure_ascii=False).encode())
            mime,size=_decode(data);digest=hashlib.sha256(data).hexdigest()
            wire.append('data:'+mime+';base64,'+base64.b64encode(data).decode('ascii'))
            bindings.append({'source_url':url,'download_url':final_url,'content_sha256':digest,
                'wire_sha256':digest,'bytes':len(data),'mime_type':mime,'width':size[0],'height':size[1]})
    finally:transport.close()
    return wire,bindings
