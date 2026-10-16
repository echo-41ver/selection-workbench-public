"""Allowlisted operational diagnostics; no raw provider messages or URLs."""
import re
OPERATIONS={'/api/v1/browser/start':'BROWSER_START','/api/v1/browser/stop':'BROWSER_STOP','/api/v1/user/list':'PROFILE_LIST','/api/v2/browser-profile/create':'PROFILE_CREATE','/api/v2/browser-profile/delete':'PROFILE_DELETE'}
def safe_code(value):
 if isinstance(value,bool):return None
 if isinstance(value,int) and abs(value)<=999999:return value
 if isinstance(value,str) and re.fullmatch(r'-?\d{1,6}',value):return int(value)
 return None
class AdsPowerAPIError(RuntimeError):
 def __init__(self,path,category,reason,provider_code=None,http_status=None,retryable=False):
  self.diagnostic={'operation':OPERATIONS.get(path.split('?',1)[0],'LOCAL_API'),'category':category,'provider_code':safe_code(provider_code),'http_status':http_status,'retryable':bool(retryable)}
  fields=' '.join(f'{k}={v}' for k,v in self.diagnostic.items() if v is not None)
  super().__init__('AdsPower Local API '+reason+' ['+fields+']')
def provider_error(path,code,message):
 text=str(message or '')
 capacity=re.search(r'number of (?:imported )?accounts exceeds (?:the )?limit of\s+(\d{1,6})(?!\d)',text,re.I)
 if re.search(r'running out of disk space|(?:not enough|insufficient|low)\s+(?:free\s+)?disk space|no space left on device|disk.{0,20}(?:full|space.{0,15}(?:low|insufficient))|磁盘.{0,15}(?:不足|已满)|存储空间不足',text,re.I):category,reason='DISK_SPACE','disk space insufficient; free space on Windows system and AdsPower data volumes before retrying'
 elif capacity:category,reason='ACCOUNT_CAPACITY',f'number of accounts exceeds limit of {capacity[1]}'
 elif re.search(r'too many requests?|request(?:s)? frequency|请求(?:频繁|频率)|访问过于频繁',text,re.I):category,reason='RATE_LIMIT','too many requests; bounded retries exhausted'
 elif re.search(r'\bnot open\b',text,re.I):category,reason='PROFILE_NOT_OPEN','profile not open'
 elif re.search(r'being used|cannot be deleted',text,re.I):category,reason='PROFILE_IN_USE','profile being used; cannot be deleted'
 elif re.search(r'permission(?:s)?\s+(?:denied|error|required)|unauthori[sz]ed|forbidden|(?:invalid|expired|missing)\s+(?:api key|token)|无权限|未授权|授权失效',text,re.I):category,reason='AUTHORIZATION','authorization or permission rejected; check AdsPower local API permissions'
 elif re.search(r'(?:SunBrowser|FlowerBrowser) \d{2,3} is not ready,\s*please to download!',text,re.I):category,reason='KERNEL_MISSING','required AdsPower browser kernel missing; run deployment kernel preparation'
 elif re.search(r'(?:kernel|firefox|chrome|内核).{0,45}(?:not (?:found|installed|available)|missing|download|不存在|未安装|下载)|(?:missing|download).{0,30}(?:kernel|firefox|chrome|内核)',text,re.I):category,reason='KERNEL_UNAVAILABLE','requested browser kernel unavailable; verify the configured kernel in AdsPower'
 elif re.search(r'proxy.{0,30}(?:fail|invalid|unavailable)|代理.{0,20}(?:失败|无效)',text,re.I):category,reason='PROXY_CONFIGURATION','proxy configuration rejected; inspect local proxy preparation'
 else:category,reason='PROVIDER_REJECTED','provider rejected request; inspect AdsPower local diagnostics'
 return AdsPowerAPIError(path,category,reason,provider_code=code)
