"""Recall-preserving image views: old centre crop, full item, uniform-background subject."""
import numpy as np
from PIL import Image, ImageOps

BASE_REVISION = 'torchvision/resnet50-imagenet1k-v2|weights=IMAGENET1K_V2|transform=official-v1'
VECTOR_REVISION = BASE_REVISION + '|views=center,whole,subject-v1|max-view-cosine-v1'
VIEW_NAMES = ('center', 'whole', 'subject')
FEATURE_DIM = 2048


def validate_features(vector, revision):
    if revision == VECTOR_REVISION:
        if vector.size != len(VIEW_NAMES)*FEATURE_DIM:
            raise ValueError('invalid multi-view vector dimensions')
        norms=np.linalg.norm(vector.reshape(len(VIEW_NAMES),FEATURE_DIM),axis=1)
        if not np.isfinite(vector).all() or not np.isfinite(norms).all() or (norms<=0).any():
            raise ValueError('invalid or zero-norm multi-view vector')


def letterbox(image):
    canvas = Image.new('RGB', (224, 224), 'white')
    fitted = ImageOps.contain(image.convert('RGB'), (208, 208), Image.Resampling.LANCZOS)
    canvas.paste(fitted, ((224-fitted.width)//2, (224-fitted.height)//2))
    return canvas


def subject_view(image):
    """Only crop a reliably uniform border; scene images keep the full frame.

    This is an extra recall view, never the sole evidence or a segmentation claim.
    """
    rgb = image.convert('RGB')
    small = ImageOps.contain(rgb, (512, 512))
    pixels = np.asarray(small, dtype=np.int16)
    border = np.concatenate((pixels[0],pixels[-1],pixels[:,0],pixels[:,-1]))
    background = np.median(border, axis=0)
    if np.mean(np.max(np.abs(border-background),axis=1) <= 24) < .95:
        return rgb
    mask = np.max(np.abs(pixels-background),axis=2) > 32
    if not .015 <= float(mask.mean()) <= .85:
        return rgb
    ys,xs = np.where(mask)
    if not len(xs):
        return rgb
    margin = max(4, int(max(xs.max()-xs.min(),ys.max()-ys.min())*.08))
    sx,sy = rgb.width/small.width,rgb.height/small.height
    bounds = (max(0,int((xs.min()-margin)*sx)),max(0,int((ys.min()-margin)*sy)),
              min(rgb.width,int((xs.max()+margin+1)*sx)),min(rgb.height,int((ys.max()+margin+1)*sy)))
    return rgb.crop(bounds)


def image_views(image):
    rgb = image.convert('RGB')
    return (rgb, letterbox(rgb), letterbox(subject_view(rgb)))


def similarity_scores(left, right, revision):
    """Use the best view pair; old centre/centre similarity remains available."""
    if revision != VECTOR_REVISION:
        return np.clip(np.einsum('ij,ij->i',left,right,optimize=True),-1,1)*100
    if left.shape != right.shape or left.shape[1] != len(VIEW_NAMES)*FEATURE_DIM:
        raise ValueError('invalid multi-view vector dimensions')
    a = left.reshape(-1,len(VIEW_NAMES),FEATURE_DIM)
    b = right.reshape(-1,len(VIEW_NAMES),FEATURE_DIM)
    an,bn = np.linalg.norm(a,axis=2,keepdims=True),np.linalg.norm(b,axis=2,keepdims=True)
    if not np.isfinite(a).all() or not np.isfinite(b).all() or (an<=0).any() or (bn<=0).any():
        raise ValueError('invalid multi-view feature')
    scores = np.einsum('nvd,nwd->nvw',a/an,b/bn,optimize=True)
    return np.clip(scores.max(axis=(1,2)),-1,1)*100
