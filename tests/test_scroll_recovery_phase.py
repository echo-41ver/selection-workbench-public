import json
from pathlib import Path
import subprocess
import pytest

ROOT=Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('instant_moves', [True,False])
def test_scroll_recovery_latches_and_observes_movement(instant_moves):
    name='visibleScrollToBottom'
    source=(ROOT/'scripts/temu_flower_collect.mjs').read_text('utf-8')
    start=source.index('async function '+name+'()')
    end=source.index('\nasync function ',start+1)
    code=r"""
const behaviors=[],events=[]; let y=0;
const seeMoreBottomOffset=100;
const scan=async()=>({scrollY:y});
const classify=()=>false;
const lightPageState=async()=>({scrollY:y,scrollHeight:1800,innerHeight:600});
const appendEvent=async(name,value)=>events.push([name,value]);
const recordCollectionScroll=async()=>{};
const rand=()=>300;
const sleep=async()=>{};
Math.random=()=>0.5;
const execute=async(script,args)=>{behaviors.push(args[1]);if(args[1]==='instant'&&MOVES)y=Math.min(1200,y+args[0]);};
FUNCTION
await NAME();
const first=behaviors.indexOf('instant');
if(first<0 || behaviors.slice(first).some(x=>x!=='instant'))throw Error('recovery not latched');
if(events.filter(x=>x[0]==='SCROLL_STALL_RECOVERY').length!==1)throw Error('repeated recovery');
if(events.some(x=>x[0]==='SCROLL_STALL_RECOVERY_VERIFIED')!==MOVES)throw Error('movement verification incorrect');
""".replace('FUNCTION',source[start:end]).replace('NAME',name).replace('MOVES',json.dumps(instant_moves))
    result=subprocess.run(['node','--input-type=module','-e',code],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
