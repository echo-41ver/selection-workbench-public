import sys
from pathlib import Path
import pytest
from urllib.parse import urlunsplit
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from collect_market_evidence import safe_failure

@pytest.mark.parametrize('kind',[TimeoutError,ConnectionError,ValueError,ImportError])
def test_market_failure_retains_type_but_never_exception_credentials(kind):
    # Construct a synthetic credential-bearing URL only inside this safety test.
    fixture=urlunsplit(('https', ':'.join(('user','password'))+'@example.com', '', 'api_key=SECRET', ''))
    result=safe_failure(kind(fixture))
    assert result['error_type']==kind.__name__
    assert result['status']=='FAILED'
    assert result['error_code']=='MARKET_PROVIDER_FAILED'
    assert 'SECRET' not in str(result) and 'password' not in str(result) and 'example.com' not in str(result)
