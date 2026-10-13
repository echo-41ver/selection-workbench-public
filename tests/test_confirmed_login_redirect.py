from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[1]


def test_exact_login_redirect_does_not_promote_generic_http_or_captcha():
    source=(ROOT/'scripts/temu_flower_collect.mjs').read_text('utf-8')
    start=source.index('function confirmedLoginRedirect(')
    end=source.index('async function guardInitialProducts',start)
    code="const classify=s=>s.block;\n"+source[start:end]+r"""
for (const [url,block,want] of [
 ['https://www.temu.com/login.html','ACCESS_DENIED',true],
 ['https://www.temu.com/login?next=abc','HTTP_BLOCK',true],
 ['https://www.temu.com/login.html','HUMAN_VERIFICATION',false],
 ['https://www.temu.com/bgn_verification.html','HUMAN_VERIFICATION',false],
 ['https://www.temu.com/search_result.html?next=/login.html','ACCESS_DENIED',false],
 ['https://www.temu.com/foo/login.html','ACCESS_DENIED',false],
 ['https://www.temu.com/login.html.extra','ACCESS_DENIED',false],
 ['invalid','ACCESS_DENIED',false]
]) { if(confirmedLoginRedirect({url,block})!==want)throw Error(url); }
"""
    result=subprocess.run(['node','--input-type=module','-e',code],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    guard=source[end:source.index('async function ',end+20)]
    assert guard.count('confirmed_login_redirect: confirmedLoginRedirect(state)')==2
    assert source.count('confirmed_login_redirect: confirmedLoginRedirect(before)')==2
