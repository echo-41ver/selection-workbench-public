"""Numerical checks for the standalone D1 API without its optional web server."""
import ast
from pathlib import Path

import pytest


@pytest.fixture(scope='module')
def attention():
    torch = pytest.importorskip('torch')
    source = Path(__file__).resolve().parents[1] / 'scripts/local_d1_api.py'
    # The standalone service uses the installed model's FastAPI environment.
    # Load its actual numerical function, without loading a model or web server.
    tree = ast.parse(source.read_text('utf-8'))
    function = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == 'chunked_causal_attention')
    scope = {'torch': torch}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), 'exec'), scope)
    return torch, scope['chunked_causal_attention']


@pytest.mark.parametrize('length,window', [(33,None), (129,None), (263,None),
                                         (263,1), (263,7), (263,129)])
def test_chunked_attention_matches_complete_causal_mask_with_grouped_heads(attention, length, window):
    torch, chunked = attention
    generator = torch.Generator().manual_seed(17)
    q = torch.randn(1,4,length,16,generator=generator)
    k = torch.randn(1,2,length,16,generator=generator)
    v = torch.randn(1,2,length,16,generator=generator)
    positions = torch.arange(length)
    allowed = positions[None,:] <= positions[:,None]
    if window is not None:
        allowed &= positions[None,:] > positions[:,None] - window
    expected = torch.nn.functional.scaled_dot_product_attention(
        q,k,v,attn_mask=allowed,scale=.25,enable_gqa=True)
    actual = chunked(q,k,v,window,.25)
    torch.testing.assert_close(actual,expected,atol=1e-6,rtol=1e-5)
