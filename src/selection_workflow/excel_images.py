"""Small, cached Amazon thumbnails for portable Excel exports."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener

from PIL import Image, ImageOps, UnidentifiedImageError


def _image_bytes(row: dict) -> bytes:
    local = str(row.get("local_image_path") or "").strip()
    if local and Path(local).is_file():
        return Path(local).read_bytes()
    url = str(row.get("main_image_url") or "").strip()
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("主图链接不是 HTTPS 地址")
    request = Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "image/avif,image/webp,image/*"})
    # Amazon image fetches never use the AdsPower/TEMU residential proxy.
    with build_opener(ProxyHandler({})).open(request, timeout=12) as response:
        if not str(response.headers.get("Content-Type") or "").lower().startswith("image/"):
            raise ValueError("主图地址未返回图片")
        payload = response.read(8_000_001)
    if len(payload) > 8_000_000:
        raise ValueError("主图超过 8MB")
    return payload


def cached_thumbnail(row: dict, cache_dir: Path) -> Path | None:
    """Return a compact JPEG image, or None while preserving source URL elsewhere."""
    identity = str(row.get("asin") or "") + "|" + str(row.get("main_image_url") or row.get("local_image_path") or "")
    if not identity.strip("|"):
        return None
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / (sha256(identity.encode("utf-8")).hexdigest() + ".jpg")
    if path.is_file():
        return path
    try:
        payload = _image_bytes(row)
        with Image.open(BytesIO(payload)) as source:
            source = ImageOps.exif_transpose(source)
            source.thumbnail((360, 360), Image.Resampling.LANCZOS)
            rgb = Image.new("RGB", source.size, "white")
            rgb.paste(source.convert("RGBA"), mask=source.convert("RGBA").getchannel("A"))
            temporary = path.with_suffix(".tmp")
            rgb.save(temporary, format="JPEG", quality=90, optimize=True)
            temporary.replace(path)
        return path
    except (OSError, ValueError, UnidentifiedImageError):
        return None


def cache_amazon_images(rows: list[dict], cache_dir: Path) -> list[Path | None]:
    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(lambda row: cached_thumbnail(row, cache_dir), rows))
