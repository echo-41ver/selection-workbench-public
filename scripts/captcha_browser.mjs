// Runs inside the workbench's ordinary Windows collectors; no agent connection.
import {spawn} from 'node:child_process';
import {existsSync} from 'node:fs';
import path from 'node:path';
import {createHash} from 'node:crypto';

const hash=value=>createHash('sha256').update(value).digest('hex');
export const captchaStatuses=new Set(['WAITING_CAPTCHA','CAPTCHA_REQUIRED','WAITING_SEARCH_VERIFICATION',
  'WAITING_EMPTY_SEARCH_VERIFICATION','WAITING_HUMAN_VERIFICATION']);

export function inspectCaptcha() {
  const visible=e=>{const r=e?.getBoundingClientRect(),s=e&&getComputedStyle(e);return r?.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'&&s.opacity!=='0';};
  const tag=(e,name)=>{e.setAttribute('data-swb-captcha',name);return `[data-swb-captcha="${name}"]`;};
  const rect=e=>{const r=e.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height};};
  const inputs=[...document.querySelectorAll('#captchacharacters,input[name="cvf_captcha_input"],input[name*="captcha" i],input[id*="captcha" i]')].filter(visible);
  if(inputs.length===1) {
    const input=inputs[0],form=input.closest('form')||input.parentElement;
    const images=[...form.querySelectorAll('img')].filter(visible);
    const buttons=[...form.querySelectorAll('button[type="submit"],input[type="submit"],button:not([type])')].filter(visible);
    if(images.length===1&&buttons.length===1&&images[0].naturalWidth>0) {
      return {kind:'normal',input:tag(input,'input'),submit:tag(buttons[0],'submit'),
        images:[{selector:tag(images[0],'image0'),rect:rect(images[0]),src:images[0].currentSrc||images[0].src,
          width:images[0].naturalWidth,height:images[0].naturalHeight}]};
    }
  }
  // Temu's documented service is the three movable picture-piece task.
  // Require drag instructions AND three explicitly draggable images; never
  // reinterpret a slider, rotation or line puzzle as that service task.
  const panels=[...document.querySelectorAll('[role="dialog"],[aria-modal="true"],[id*="captcha" i],[class*="captcha" i],[id*="verify" i],[class*="verify" i]')].filter(visible);
  for(const panel of panels) {
    const text=panel.innerText||'';
    if(!/drag.{0,60}(?:image|picture|piece)|拖动.{0,20}(?:图片|图块|拼图)|拖動.{0,20}(?:圖片|圖塊|拼圖)/i.test(text)
      || /rotate|rotation|slide|connect|旋转|旋轉|滑块|滑塊|连线|連線/i.test(text))continue;
    const pieces=[...panel.querySelectorAll('img[draggable="true"],[draggable="true"] img')].filter(visible);
    const backgrounds=[...panel.querySelectorAll('img')].filter(e=>visible(e)&&!pieces.includes(e)&&e.naturalWidth>=150&&e.naturalHeight>=100);
    if(pieces.length!==3||backgrounds.length!==1||pieces.some(e=>!e.naturalWidth))continue;
    const images=[backgrounds[0],...pieces].map((e,i)=>({selector:tag(e,'image'+i),rect:rect(e),src:e.currentSrc||e.src,width:e.naturalWidth,height:e.naturalHeight}));
    const buttons=[...panel.querySelectorAll('button,[role="button"]')].filter(e=>visible(e)&&/^(?:submit|confirm|verify|确定|确认|提交|驗證|验证)$/i.test((e.innerText||'').trim()));
    return {kind:'temu',images,submit:buttons.length===1?tag(buttons[0],'submit'):null};
  }
  const challenge=[...document.querySelectorAll('iframe[src*="captcha"],iframe[src*="geetest"],iframe[src*="verification"],.geetest_panel,.geetest_holder,#captcha-container')].some(visible)
    || panels.some(p=>/verify.{0,15}human|captcha|security verification|complete the verification|drag|rotate|人机|验证码|安全验证|请完成验证|滑块|拼图|连线/i.test(p.innerText||''))
    || /bgn_verification|validateCaptcha|security[-_]?verification/i.test(location.pathname)
    || /robot check|enter the characters you see/i.test(document.title+' '+(document.body?.innerText||'').slice(0,1000));
  return challenge?{kind:'unsupported',images:[]}:null;
}

export function captchaRequest(root, request) {
  const python=process.env.SELECTION_PYTHON || [path.join(root,'.venv/Scripts/python.exe'),path.join(root,'runtime/python/python.exe')].find(existsSync) || 'python';
  return new Promise(resolve=>{
    const child=spawn(python,[path.join(root,'scripts/captcha_request.py')],{cwd:root,windowsHide:true,stdio:['pipe','pipe','pipe'],
      env:{...process.env,PYTHONUTF8:'1',PYTHONIOENCODING:'utf-8'}});
    let output='',done=false;
    const finish=result=>{if(done)return;done=true;clearTimeout(timer);resolve(result);};
    const failed=()=>finish({status:'SERVICE_FAILED',message:'2Captcha 服务连接失败；已保留当前现场。'});
    const timer=setTimeout(()=>{child.kill();failed();},55000);
    child.stdout.on('data',chunk=>{output+=chunk;if(output.length>20000){child.kill();failed();}});
    // Never forward stderr: Python/HTTP errors can contain secrets.
    child.stderr.resume();child.on('error',failed);child.stdin.on('error',failed);
    child.on('close',()=>{try{const value=JSON.parse(output);if(typeof value.status!=='string')return failed();finish(value);}catch{failed();}});
    child.stdin.end(JSON.stringify(request));
  });
}

export function parseTemuCoordinates(answer, image) {
  let points;
  try {points=JSON.parse(answer);points=points.coordinates||points;}catch {
    points=[...String(answer).matchAll(/x=(\d+),y=(\d+)/g)].map(m=>({x:Number(m[1]),y:Number(m[2])}));
  }
  if(!Array.isArray(points)||points.length!==3||points.some(p=>!Number.isFinite(p.x)||!Number.isFinite(p.y)
    ||p.x<0||p.y<0||p.x>=image.width||p.y>=image.height))throw new Error('INVALID_CAPTCHA_COORDINATES');
  return points;
}

export function webdriverCaptchaAdapter(wd,execute) {
  return {
    inspect:()=>execute(`return (${inspectCaptcha.toString()})();`,[],'captcha_inspect'),
    images:async challenge=>{
      const images=[];
      for(const item of challenge.images) {
        const element=await wd('POST','/element',{using:'css selector',value:item.selector});
        const id=element['element-6066-11e4-a52e-4f735466cecf'];
        const encoded=await wd('GET',`/element/${id}/screenshot`),buffer=Buffer.from(encoded,'base64');
        item.width=buffer.readUInt32BE(16);item.height=buffer.readUInt32BE(20);images.push(encoded);
      }
      return images;
    },
    apply:async(challenge,answer)=>{
      const element=async selector=>(await wd('POST','/element',{using:'css selector',value:selector}))['element-6066-11e4-a52e-4f735466cecf'];
      if(challenge.kind==='normal') {
        const id=await element(challenge.input);
        await wd('POST',`/element/${id}/clear`,{});
        await wd('POST',`/element/${id}/value`,{text:answer});
      } else {
        const points=parseTemuCoordinates(answer,challenge.images[0]);
        for(let i=0;i<points.length;i++) {
          const piece=await wd('GET',`/element/${await element(challenge.images[i+1].selector)}/rect`);
          const bg=await wd('GET',`/element/${await element(challenge.images[0].selector)}/rect`);
          await wd('POST','/actions',{actions:[{type:'pointer',id:'captcha-mouse',parameters:{pointerType:'mouse'},actions:[
            {type:'pointerMove',duration:100,origin:'viewport',x:Math.round(piece.x+piece.width/2),y:Math.round(piece.y+piece.height/2)},
            {type:'pointerDown',button:0},{type:'pause',duration:150},
            {type:'pointerMove',duration:700,origin:'viewport',x:Math.round(bg.x+points[i].x/challenge.images[0].width*bg.width),y:Math.round(bg.y+points[i].y/challenge.images[0].height*bg.height)},
            {type:'pointerUp',button:0}]}]});
        }
      }
      if(challenge.submit)await wd('POST',`/element/${await element(challenge.submit)}/click`,{});
    },
  };
}

export function createCaptchaHandler({root,scope,browserIdentity='',adapter,request=r=>captchaRequest(root,r),
  onStatus=async()=>{},shouldStop=async()=>false,isReleased=async()=>false,
  sleep=ms=>new Promise(r=>setTimeout(r,ms)),now=Date.now}) {
  let handling=false,lastStatus='';
  const notify=async value=>{
    // Only fixed state/message metadata reaches logs and progress.
    if(lastStatus!==value.status){lastStatus=value.status;await onStatus({status:value.status,message:value.message,
      browser_preserved:true,session_preserved:true,resume_requires_session_rotation:false});}
  };
  return async()=>{
    if(handling)return false;
    handling=true;
    try {
      let challenge=await adapter.inspect();
      if(!challenge)return await isReleased();
      if(challenge.kind==='unsupported') {
        await notify({status:'UNSUPPORTED',message:'当前验证码题型尚未适配；保留原环境和断点，请在原窗口处理。'});
        return false;
      }
      const images=await adapter.images(challenge);
      const fingerprint=hash(JSON.stringify([browserIdentity,challenge.kind,images]));
      const input={scope,fingerprint,kind:challenge.kind,images};
      let result;
      do {
        if(await shouldStop()){await notify({status:'STOPPED',message:'已停止解题等待，当前浏览器和付费任务记录保留。'});return false;}
        result=await request(input);await notify(result);
        if(result.status==='PENDING') {
          for(let i=0;i<10&&!await shouldStop();i++)await sleep(1000);
          if(!await adapter.inspect()&&await isReleased())return true;
        }
      } while(result.status==='PENDING');
      if(result.status!=='ANSWER_READY')return false;
      const current=await adapter.inspect();
      if(!current)return await isReleased();
      // Never enter an answer on a replaced/changed challenge.
      if(hash(JSON.stringify([browserIdentity,current.kind,await adapter.images(current)]))!==fingerprint)return false;
      const answer=result.answer;
      if(current.kind==='normal'&&(typeof answer!=='string'||!/^.{1,64}$/u.test(answer)))return false;
      result=await request({action:'claim',scope,fingerprint});
      if(result.status!=='APPLYING')return false;
      await adapter.apply(current,answer);
      const deadline=now()+20000;
      let passed=false;
      while(now()<deadline&&!await shouldStop()) {
        await sleep(500);
        if(!await adapter.inspect()&&await isReleased()){passed=true;break;}
      }
      if(await shouldStop())return false; // Keep APPLYING: never submit the answer twice after a stop.
      result=await request({action:'verify',scope,fingerprint,passed});await notify(result);
      return passed;
    } finally {handling=false;}
  };
}
