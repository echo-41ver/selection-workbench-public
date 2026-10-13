// The first driver request can fail before collector maps are initialized.
// Publish a fresh local-control state while keeping every saved product byte.
export async function readInitialWindowHandles(request,{readCheckpoint,saveCheckpoint,appendEvent,now}) {
 try{return await request('GET','/window/handles');}
 catch(error){
  if(!['CONNECTION_FAILED','DRIVER_ERROR','INVALID_RESPONSE'].includes(error?.kind))throw error;
  const previous=await readCheckpoint();
  const row={...previous,status:'STOPPED_BROWSER_CONTROL_FAILURE',updated_at:now(),
   phase:'binding_preflight',previous_browser_status:previous.status,
   browser_preserved:true,proxy_preserved:true,manual_action_required:false,automatic_retry:true,
   automatic_recovery_eligible:false,resume_requires_session_rotation:false,automatic_resume_pending:false,
   command_execution_unknown:false,command_method:'GET',command_route:'/window/handles',
   keyword_complete:false,platform_complete:false,completion_basis:null,completion_scope:null,coverage_claim:null,
   confirmed_login_redirect:false,http_block:null,http_status:null,
   message:'本地浏览器控制连接失效，保留原窗口并等待有限重绑定'};
  await saveCheckpoint(row);await appendEvent('STOPPED_BROWSER_CONTROL_FAILURE',{phase:row.phase,error_kind:error.kind});
  throw error;
 }
}
