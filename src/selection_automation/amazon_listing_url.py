"""One Amazon Best Sellers URL contract for authoring and execution."""
import re
from urllib.parse import urlparse


def listing_identity(url):
    if not isinstance(url,str) or any(ord(c)<32 for c in url):
        return None
    try:
        parsed=urlparse(url)
        if (parsed.scheme!='https' or parsed.hostname not in ('www.amazon.com','amazon.com')
                or parsed.username or parsed.password or parsed.port not in (None,443)):
            return None
        match=re.search(r'/(?:zgbs|gp/bestsellers)(?=/|$)(.*)',parsed.path)
        if not match:
            return None
        path=match[1].split('/ref=')[0].strip('/')
        if path and not re.fullmatch(r'[A-Za-z0-9_-]+(?:/\d+)?',path):
            return None
        return 'https://www.amazon.com/zgbs'+('/'+path if path else '')
    except ValueError:
        return None
