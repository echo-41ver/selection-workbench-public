"""Bounded, secret-safe preparation of vendor FlowerBrowser kernels on the existing API."""
import shutil,time,urllib.parse
from pathlib import Path
REQUIRED_KERNEL={'type':'firefox','version':'153'}
STATES={'pending','downloading','installing','completed','failed'}

def inventory(base,api,required=REQUIRED_KERNEL):
 result=api(base,'/api/v2/browser-profile/kernels?'+urllib.parse.urlencode({'kernel_type':required['type'].capitalize()}),timeout=10)
 items=(result.get('data') or {}).get('list')
 if not isinstance(items,list):raise RuntimeError('KERNEL_INVENTORY_INVALID')
 item=next((x for x in items if x.get('kernel_type','').lower()==required['type'] and str(x.get('kernel'))==required['version']),None)
 if item is None:raise RuntimeError('KERNEL_VERSION_UNAVAILABLE')
 return {'required':dict(required),'installed':item.get('is_downloaded') is True}

def ensure_kernel(base,api,root,*,required=REQUIRED_KERNEL,deadline_seconds=900,min_free_bytes=2*1024**3,progress=None,clock=time.monotonic,sleep=time.sleep,disk_usage=shutil.disk_usage):
 parsed=urllib.parse.urlsplit(base)
 if parsed.hostname not in {'127.0.0.1','localhost','::1'} or parsed.username or parsed.password or parsed.query:raise RuntimeError('KERNEL_API_MUST_BE_LOOPBACK')
 if required['type'] not in {'firefox','chrome'} or not str(required['version']).isdigit():raise RuntimeError('KERNEL_REQUIREMENT_INVALID')
 current=inventory(base,api,required)
 if current['installed']:return current
 root=Path(root).resolve()
 volumes={root.anchor,Path.home().anchor}
 for volume in volumes:
  if disk_usage(volume).free<min_free_bytes:raise RuntimeError('KERNEL_DISK_SPACE')
 until=clock()+deadline_seconds
 body={'kernel_type':required['type'].capitalize(),'kernel_version':required['version']}
 while clock()<until:
  # Same POST polls/resumes the vendor-managed operation, per official CLI contract.
  result=api(base,'/api/v2/browser-profile/download-kernel',method='POST',body=body,timeout=20)
  data=result.get('data') or {};status=data.get('status')
  if status not in STATES:raise RuntimeError('KERNEL_STATUS_UNKNOWN')
  if progress:
   try:percent=max(0,min(100,float(data.get('progress') or 0)))
   except (TypeError,ValueError):percent=0
   progress({'kernel':dict(required),'status':status,'progress':percent})
  if status=='failed':raise RuntimeError('KERNEL_DOWNLOAD_FAILED')
  if status=='completed':
   current=inventory(base,api,required)
   if not current['installed']:raise RuntimeError('KERNEL_COMPLETION_UNVERIFIED')
   return current
  sleep(3)
 raise RuntimeError('KERNEL_DEADLINE_EXCEEDED_PARTIAL_DOWNLOAD_PRESERVED')
