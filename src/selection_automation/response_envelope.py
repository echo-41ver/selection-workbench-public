"""Narrow lossless framing compatibility; never fills a visual fact."""
import json,re
VERSION='lossless-envelope-v2-syntax-only'
class NotApplicable(ValueError):pass
def unique_object(pairs):
    out={}
    for key,value in pairs:
        if key in out:raise ValueError('Duplicate visual JSON key; no lossless normalization permitted')
        out[key]=value
    return out
def parse_envelope(text):
    payload=text.strip();changes=[]
    fence=re.fullmatch(r'```(?:json)?\s*\n(.*)\n```',payload,re.DOTALL|re.I)
    if fence:payload=fence.group(1)
    try:raw=json.loads(payload,object_pairs_hook=unique_object)
    except json.JSONDecodeError:
        prefix=re.match(r'^\s*\{\s*"candidates"\s*:\s*\[',payload)
        if not prefix:raise NotApplicable('Not an exact candidates envelope') from None
        decoder=json.JSONDecoder(object_pairs_hook=unique_object);cursor=prefix.end()
        while True:
            while cursor<len(payload) and payload[cursor].isspace():cursor+=1
            try:item,end=decoder.raw_decode(payload,cursor)
            except json.JSONDecodeError:raise NotApplicable('Incomplete candidate JSON; no repair permitted') from None
            if not isinstance(item,dict):raise NotApplicable('Candidate must remain a complete object')
            cursor=end
            while cursor<len(payload) and payload[cursor].isspace():cursor+=1
            if cursor<len(payload) and payload[cursor]==',':cursor+=1;continue
            if re.fullmatch(r'\}\s*\]\s*\}\s*',payload[cursor:]):
                raw=json.loads(payload[:cursor]+payload[cursor+1:],object_pairs_hook=unique_object)
                changes.append({'operation':'REMOVE_ONE_EXTRA_CONTAINER_CLOSING_BRACE','position':cursor})
                break
            raise NotApplicable('Unrecognized malformed envelope; no repair permitted')
    return raw,changes
