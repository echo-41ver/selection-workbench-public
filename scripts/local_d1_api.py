"""Native D1 API: shared read-only weights and a CUDA stream per request."""
import base64
import io
import json
import os
import threading
import time
import traceback
from contextlib import asynccontextmanager, nullcontext

import torch
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field

from runtime import ROOT, load_model

MAX_CONCURRENCY = int(os.environ.get('D1_MAX_CONCURRENCY', '0'))
if MAX_CONCURRENCY < 0:
    raise ValueError('D1_MAX_CONCURRENCY must be zero or a positive integer')
# Zero delegates dispatch to the workbench's existing concurrency control.
slots = threading.BoundedSemaphore(MAX_CONCURRENCY) if MAX_CONCURRENCY else nullcontext()
stats_lock = threading.Lock()
streams = []
active = peak_active = completed = failed = 0
model = None
API_VERSION = 'swb-native-d1-concurrent-v1'


def chunked_causal_attention(q, k, v, window, scale):
    """Exact causal SDPA, with bounded query workspaces on Windows math kernels."""
    parts = []
    for start in range(0, q.shape[2], 128):
        end = min(start + 128, q.shape[2])
        keys = torch.arange(end, device=q.device)[None, :]
        queries = torch.arange(start, end, device=q.device)[:, None]
        allowed = keys <= queries
        if window is not None:
            allowed &= keys > queries - window
        parts.append(torch.nn.functional.scaled_dot_product_attention(
            q[:, :, start:end], k[:, :, :end], v[:, :, :end],
            attn_mask=allowed, scale=scale, enable_gqa=True))
    return torch.cat(parts, dim=2)


@asynccontextmanager
async def lifespan(app):
    global model
    print('Loading local d1-3B (NF4 on CUDA)...', flush=True)
    if torch.cuda.is_available():
        # WDDM otherwise spills large attention workspaces into shared RAM.
        torch.cuda.set_per_process_memory_fraction(.90)
    model = load_model()
    if torch.cuda.is_available() and not torch.backends.cuda.is_flash_attention_available():
        from importlib import import_module
        hybrid = import_module(model.__class__.__module__.rsplit('.', 1)[0] + '.hybrid')
        hybrid.CHUNK = 128
        hybrid.causal_attention = chunked_causal_attention
    # Initialize the shared lazy tokenizer/processor before concurrent requests.
    model.engine._image_markup(1)
    if torch.cuda.is_available():
        torch.cuda.synchronize()
    print(f'Ready: http://127.0.0.1:7863; inference concurrency={MAX_CONCURRENCY or "caller-controlled"}', flush=True)
    yield


app = FastAPI(title='Local d1-3B Decision API', lifespan=lifespan)


class DecisionRequest(BaseModel):
    state: str | dict | list | int | float | bool | None = None
    questions: dict = Field(min_length=1, max_length=10)
    image_base64: str | None = Field(default=None, max_length=8_000_000)


@app.get('/health')
def health():
    with stats_lock:
        stats = {'limit': MAX_CONCURRENCY or None, 'active': active, 'peak_active': peak_active,
                 'completed': completed, 'failed': failed}
    gpu = ({'allocated_mib': round(torch.cuda.memory_allocated() / 1048576, 1),
            'reserved_mib': round(torch.cuda.memory_reserved() / 1048576, 1),
            'peak_allocated_mib': round(torch.cuda.max_memory_allocated() / 1048576, 1)}
           if torch.cuda.is_available() else None)
    return {'ready': model is not None, 'model': 'LiquidAI/d1-3B',
            'revision': '051bcc464b01b9f92942b364d9586b0ef5912432',
            'device': str(next(model.parameters()).device) if model is not None else None,
            'quantization': 'NF4' if torch.cuda.is_available() else 'float32',
            'api_version': API_VERSION, 'concurrency': stats, 'gpu': gpu}


@app.post('/v1/decide')
def decide(request: DecisionRequest):
    global active, peak_active, completed, failed
    images = []
    if request.image_base64:
        try:
            raw = request.image_base64.split(',', 1)[-1]
            image = Image.open(io.BytesIO(base64.b64decode(raw, validate=True)))
            if image.width * image.height > 16_000_000:
                raise ValueError('Image exceeds 16 megapixels')
            images = [image.convert('RGB')]
        except (ValueError, OSError, UnidentifiedImageError) as exc:
            raise HTTPException(422, f'Invalid image: {exc}') from exc
    from importlib import import_module
    prompt = import_module(model.__class__.__module__.rsplit('.', 1)[0] + '.prompt')
    questions = [prompt.as_question(q) for q in request.questions.values()]
    if model.engine.tokens(request.state, questions) > 4096:
        raise HTTPException(422, 'Text exceeds the local limit of 4096 tokens')
    with slots, torch.inference_mode():
        with stats_lock:
            active += 1
            peak_active = max(peak_active, active)
        started = time.perf_counter()
        ok = False
        stream = None
        try:
            with stats_lock:
                stream = ((streams.pop() if streams else torch.cuda.Stream())
                          if torch.cuda.is_available() else None)
            with torch.cuda.stream(stream) if stream is not None else nullcontext():
                result = model.system_one(request.state, request.questions, images=images or None)
            if stream is not None:
                stream.synchronize()
            result['elapsed_ms'] = round((time.perf_counter() - started) * 1000, 2)
            ok = True
            return result
        except torch.OutOfMemoryError as exc:
            traceback.print_exc()
            print(json.dumps({'event': 'inference_oom', 'message': str(exc)}), flush=True)
            raise HTTPException(503, 'GPU memory is full. Reduce inference concurrency or use a GPU with more memory.') from exc
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise HTTPException(422, str(exc)) from exc
        finally:
            with stats_lock:
                if stream is not None:
                    streams.append(stream)
                active -= 1
                completed += int(ok)
                failed += int(not ok)
            print(json.dumps({'event': 'inference_finished', 'ok': ok,
                              'elapsed_ms': round((time.perf_counter() - started) * 1000, 2)}), flush=True)


@app.get('/', response_class=HTMLResponse)
def index():
    return (ROOT / 'index.html').read_text(encoding='utf-8')


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=7863, workers=1)
