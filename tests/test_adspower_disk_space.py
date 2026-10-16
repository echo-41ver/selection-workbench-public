import json,sys
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'scripts'),str(ROOT/'src')]
from selection_automation.adspower_diagnostics import provider_error,AdsPowerAPIError
from replace_proxyshare_worker_environment import start_profile_with_retry

@pytest.mark.parametrize('message',[
 'You are running out of disk space. Delete cache by clicking Delete Cache',
 'Not enough disk space', 'Insufficient free disk space',
 'No space left on device', 'Disk is full', '磁盘可用空间不足',
])
def test_storage_failure_classified_without_echoing_provider_secrets(message):
 error=provider_error('/api/v1/browser/start?user_id=PRIVATE',-1,message+' key=SECRET password=PASS')
 assert error.diagnostic['category']=='DISK_SPACE'
 assert error.diagnostic['provider_code']==-1
 assert error.diagnostic['retryable'] is False
 assert all(token not in str(error)+json.dumps(error.diagnostic) for token in ['PRIVATE','SECRET','PASS','Delete Cache'])

def test_storage_failure_does_not_retry_or_recreate_existing_profile():
 calls=[]
 def api(base,path,**kwargs):
  calls.append(path)
  raise provider_error(path,-1,'You are running out of disk space')
 with pytest.raises(AdsPowerAPIError):
  start_profile_with_retry('fixture','owned-profile',api=api)
 assert len(calls)==1 and '/browser/start?' in calls[0]

def test_unrelated_space_text_does_not_claim_disk_is_full():
 assert provider_error('/api/v1/browser/start',-1,'profile name has a space').diagnostic['category']=='PROVIDER_REJECTED'
