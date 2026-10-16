from pathlib import Path
import subprocess

ROOT = Path(__file__).parents[1]


def test_open_feedback_survives_poll_and_failure_offers_download():
    html = (ROOT / 'web/index.html').read_text(encoding='utf-8')
    code = html[html.index('const excelOpening=new Set()'):html.index('\nfor(const button of document.querySelectorAll',html.index('async function openExcel'))]
    script = """
const nodes=new Map();
const $=id=>{if(!nodes.has(id))nodes.set(id,{style:{},textContent:'',hidden:true});return nodes.get(id);};
const buttons=new Map();
for(const kind of ['amazon','temu','layer12','layer123'])buttons.set(kind,{disabled:false,textContent:'打开 '+kind});
const document={querySelector:selector=>buttons.get(selector.match(/data-excel="([^"]+)"/)[1])};
let busy=false,statusFresh=true;
let lastStatus={excel_files:{layer123:{available:true,filename:'最新.xlsx',updated_at:1000}}};
const feedback=()=>{};
let resolveRequest,requests=0;
let fetch=async()=>{requests++;return new Promise(resolve=>{resolveRequest=resolve;});};
CODE
(async()=>{
 const pending=openExcel('layer123');
 if(!$('excel-open-status').textContent.includes('正在打开')||!$('excel-open-status').textContent.includes('文件更新于'))throw Error('no immediate feedback');
 if($('excel-download').hidden||$('excel-download').href!=='/api/excel/layer123')throw Error('no download fallback');
 excelButtons(lastStatus);
 if(!buttons.get('layer123').disabled)throw Error('poll reenabled in-flight button');
 await openExcel('layer123');
 if(requests!==1)throw Error('duplicate open request');
 resolveRequest({ok:true,json:async()=>({ok:true,filename:'最新.xlsx',updated_at:1000})});
 await pending;
 if(!$('excel-open-status').textContent.includes('已交给本机表格程序打开')||buttons.get('layer123').disabled)throw Error('no success feedback');
 fetch=async()=>({ok:false,json:async()=>({error:{message:'本机打开失败'}})});
 await openExcel('layer123');
 if(!$('excel-open-status').textContent.includes('下载最新 Excel')||$('excel-download').hidden)throw Error('missing failure fallback');
 fetch=async()=>{const error=Error('aborted');error.name='AbortError';throw error;};
 await openExcel('layer123');
 if(!$('excel-open-status').textContent.includes('超时')||buttons.get('layer123').disabled)throw Error('timeout did not recover');
})().catch(error=>{console.error(error);process.exitCode=1;});
""".replace('CODE', code)
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
