export class BrowserControlError extends Error {
  constructor(kind, method, route) {
    super(`Local browser control failed: ${kind} (${method} ${route})`);
    this.kind=kind;this.method=method;this.route=route;
  }
}

export async function browserControlRequest({base,session,method,suffix,body,
  timeoutMs=60000,fetchImpl=fetch}) {
  const route=suffix.replace(/\/element\/[^/]+/,'/element/:id');
  let response,json,timer;
  const abort=new AbortController();
  const deadline=new Promise((_,reject)=>{timer=setTimeout(()=>{abort.abort();reject(new BrowserControlError('TIMEOUT',method,route));},timeoutMs);});
  try {
    const operation=(async()=>{response=await fetchImpl(`${base}/session/${session}${suffix}`,{
      method,headers:body===undefined?undefined:{'content-type':'application/json'},
      body:body===undefined?undefined:JSON.stringify(body),signal:abort.signal,
    });
    json=await response.json();})();
    await Promise.race([operation,deadline]);
  } catch(error) {
    if(error instanceof BrowserControlError)throw error;
    const kind=['TimeoutError','AbortError'].includes(error?.name)?'TIMEOUT'
      :error instanceof SyntaxError?'INVALID_RESPONSE':'CONNECTION_FAILED';
    throw new BrowserControlError(kind,method,route);
  } finally {clearTimeout(timer);}
  if(!response.ok||json?.value?.error)throw new Error(`${method} ${route}: ${json?.value?.error||response.status} ${json?.value?.message||''}`);
  return json.value;
}


export function createBrowserControlClient({base,session,fetchImpl=fetch,onTimeout=async()=>{},commandTimeoutMs=30000,navigationTimeoutMs=60000}) {
  let uncertain=null;
  return async (method,suffix,body,{commandLabel}={})=>{
    // Aborting the HTTP wait does not prove the queued driver command was cancelled.
    if(uncertain)throw uncertain;
    try{return await browserControlRequest({base,session,method,suffix,body,fetchImpl,
      timeoutMs:method==='POST'&&suffix==='/url'?navigationTimeoutMs:commandTimeoutMs});}
    catch(error){
      if(error instanceof BrowserControlError&&error.kind==='TIMEOUT'){
        error.commandLabel=/^[a-z0-9_-]{1,64}$/.test(String(commandLabel||''))?commandLabel:null;
        uncertain=error;
        await onTimeout(error);
      }
      throw error;
    }
  };
}
