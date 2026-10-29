"""Bounded discovery from local ownership evidence; never probes external targets."""
import ipaddress
import re
import socket
import time

CLIENTS = {'clash.exe','clash-meta.exe','mihomo.exe','verge-mihomo.exe','sing-box.exe','v2ray.exe',
           'xray.exe','ss-local.exe','shadowsocks.exe','shadowsocksr.exe',
           'nekoray.exe','hiddify.exe'}
TUN_CLIENTS = {'wireguard.exe','openvpn.exe','tailscaled.exe'}
MAX_CANDIDATES = 12
TIMEOUT = .35

def loopback(host):
    if host == 'localhost':return True
    try:return ipaddress.ip_address(host).is_loopback
    except ValueError:return False

def endpoint(value):
    # Credentials, URLs and PAC scripts are never accepted as local endpoints.
    if not isinstance(value,str) or any(c in value for c in '/@?#'):return None
    match=re.fullmatch(r'\[([^]]+)\]:(\d+)|([^:]+):(\d+)',value.strip())
    if not match:return None
    host=match[1] or match[3];port=int(match[2] or match[4])
    return (host,port) if loopback(host) and 1<=port<=65535 else None

def system_proxy_metadata():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,r'Software\Microsoft\Windows\CurrentVersion\Internet Settings') as key:
            def get(name,default=None):
                try:return winreg.QueryValueEx(key,name)[0]
                except FileNotFoundError:return default
            server=get('ProxyServer','') if get('ProxyEnable',0) else ''
            endpoints=[]
            for item in str(server).split(';'):
                kind,sep,address=item.partition('=')
                parsed=endpoint(address if sep else kind)
                if parsed:endpoints.append({'host':parsed[0],'port':parsed[1],
                    'declared_protocol':'SOCKS5' if sep and kind.lower()=='socks' else 'HTTP'})
            # Retain only presence, never fetch/execute PAC or log its URL.
            return {'endpoints':endpoints,'pac_present':bool(get('AutoConfigURL')),'wpad_enabled':bool(get('AutoDetect',0))}
    except (OSError,ImportError):return {'endpoints':[],'pac_present':False,'read_unavailable':True}

def snapshot():
    import psutil
    current=psutil.Process().username().casefold()
    processes=[]
    for proc in psutil.process_iter(['pid','name']):
        name=(proc.info['name'] or '').casefold()
        if name not in CLIENTS|TUN_CLIENTS:continue
        try:
            if proc.username().casefold()!=current:continue
            argv=proc.cmdline()
            declarations={}
            for index,arg in enumerate(argv[:-1]):
                if arg in {'--socks-port','--http-port','--mixed-port'} and argv[index+1].isdigit():
                    declarations[int(argv[index+1])]={'--socks-port':'SOCKS5','--http-port':'HTTP','--mixed-port':'MIXED'}[arg]
            listeners=[]
            for connection in proc.net_connections(kind='tcp'):
                if connection.status!='LISTEN':continue
                host=connection.laddr.ip
                if host=='0.0.0.0':host='127.0.0.1'
                elif host=='::':host='::1'
                if not loopback(host):continue
                listeners.append({'host':host,'port':connection.laddr.port,
                    'declared_protocol':declarations.get(connection.laddr.port)})
            processes.append({'pid':proc.pid,'created':proc.create_time(),'name':name,
                'listeners':listeners,'declared_ports':sorted(declarations),
                'tun_only':name in TUN_CLIENTS or '--tun' in argv})
        except (psutil.AccessDenied,psutil.NoSuchProcess):continue
    return {'processes':processes,'system':system_proxy_metadata()}

def owned(candidate):
    """Recheck PID generation and listener ownership; port reuse is not evidence."""
    import psutil
    try:
        proc=psutil.Process(candidate['pid'])
        if abs(proc.create_time()-candidate['created'])>.01:return False
        if proc.username().casefold()!=psutil.Process().username().casefold():return False
        if proc.name().casefold()!=candidate['name']:return False
        return any(c.status=='LISTEN' and c.laddr.port==candidate['port']
            and c.laddr.ip in {candidate['host'],'0.0.0.0' if candidate['host']=='127.0.0.1' else '::'}
            for c in proc.net_connections(kind='tcp'))
    except (psutil.AccessDenied,psutil.NoSuchProcess):return False

def _receive(sock,limit=2048):
    data=b''
    while len(data)<limit:
        chunk=sock.recv(min(1024,limit-len(data)))
        if not chunk:break
        data+=chunk
        if b'\r\n\r\n' in data or (data[:1]==b'\x05' and len(data)>=2):break
    return data

def probe(candidate,connect=socket.create_connection):
    host,port=candidate['host'],candidate['port']
    if not loopback(host) or not 1<=port<=65535:return {'protocol':None}
    socks=False;http=False;authentication=False
    try:
        with connect((host,port),timeout=TIMEOUT) as sock:
            sock.settimeout(TIMEOUT);sock.sendall(b'\x05\x01\x00')
            reply=_receive(sock,2);socks=reply==b'\x05\x00';authentication=reply==b'\x05\x02'
    except OSError:pass
    try:
        with connect((host,port),timeout=TIMEOUT) as sock:
            sock.settimeout(TIMEOUT)
            # Invalid local port: protocol identification only, no remote target.
            sock.sendall(b'CONNECT 127.0.0.1:0 HTTP/1.1\r\nHost: 127.0.0.1:0\r\nConnection: close\r\n\r\n')
            reply=_receive(sock)
            status=re.match(rb'HTTP/1\.[01] (\d{3})(?: |\r)',reply)
            if status:
                code=int(status[1]);authentication=authentication or code==407
                declared=candidate.get('declared_protocol') in {'HTTP','MIXED'}
                proxy_header=b'\r\nproxy-agent:' in reply.lower()
                # Clash can acknowledge CONNECT before the invalid target fails.
                # Require its tunnel response, not an ordinary web server's 200 OK.
                tunnel_established=(code==200 and b'\r\n\r\n' in reply and
                    re.match(rb'HTTP/1\.[01] 200 Connection established\r\n',reply,re.I) is not None)
                # A generic HTTP controller/web server is not an HTTP proxy.
                http=(code in {400,502,503} or tunnel_established) and (declared or proxy_header)
    except OSError:pass
    return {'protocol':'MIXED' if socks and http else 'SOCKS5' if socks else 'HTTP' if http else None,
            'authentication_required':authentication and not socks}

def discover(evidence=None,check_owner=owned,protocol_probe=probe,clock=time.monotonic):
    evidence=snapshot() if evidence is None else evidence
    system=evidence.get('system',{});candidates=[];seen=set();warnings=[]
    for proc in evidence.get('processes',[]):
        if proc.get('name') not in CLIENTS|TUN_CLIENTS:continue
        for item in proc.get('listeners',[]):
            if not loopback(item['host']):continue
            key=(proc['pid'],item['port'],item['host'])
            if key in seen:continue  # IPv4/IPv6 listeners of one port are one route.
            seen.add(key);row={**item,**{k:proc[k] for k in ['pid','created','name']}}
            for setting in system.get('endpoints',[]):
                if setting['port']==row['port'] and setting['host'] in {row['host'],'localhost'}:
                    row['declared_protocol']=setting.get('declared_protocol')
            candidates.append(row)
        if any(port not in {c['port'] for c in proc.get('listeners',[])} for port in proc.get('declared_ports',[])):
            warnings.append('客户端配置端口与实际监听不符')
    if len(candidates)>MAX_CANDIDATES:
        # Other tools may expose many proxy ports. The current Windows proxy
        # still identifies a bounded, explicit route without probing them all.
        candidates=[c for c in candidates if any(s['port']==c['port'] and
            s['host'] in {c['host'],'localhost'} for s in system.get('endpoints',[]))]
        if not candidates or len(candidates)>MAX_CANDIDATES:
            return {'status':'TOO_MANY_CANDIDATES','candidates':[],'warnings':warnings,
                    'running_client_count':len(evidence.get('processes',[]))}
        warnings.append('本机代理监听较多，仅核验当前系统代理指定的客户端端口')
    ready=[];deadline=clock()+8
    for candidate in sorted(candidates,key=lambda c:(not bool(c.get('declared_protocol')),c['pid'],c['port'])):
        if clock()>=deadline:return {'status':'DISCOVERY_TIMEOUT','candidates':[],'warnings':warnings}
        if not check_owner(candidate):continue
        result=protocol_probe(candidate)
        if not check_owner(candidate):continue
        if result.get('protocol') and not result.get('authentication_required'):
            equivalent=next((c for c in ready if c['pid']==candidate['pid'] and c['port']==candidate['port'] and c['protocol']==result['protocol']),None)
            if equivalent:equivalent.setdefault('host_aliases',[equivalent['host']]).append(candidate['host'])
            else:ready.append({**candidate,'protocol':result['protocol']})
        elif result.get('authentication_required'):warnings.append('检测到需要凭据的本机代理，未自动使用')
    status='READY' if len(ready)==1 else 'AMBIGUOUS' if len(ready)>1 else 'TUN_ONLY' if any(p.get('tun_only') for p in evidence.get('processes',[])) else 'NO_COMPATIBLE_PROXY'
    if system.get('endpoints') and not candidates:warnings.append('系统代理未能对应到当前用户的已知代理进程')
    if system.get('pac_present') or system.get('wpad_enabled'):warnings.append('检测到 PAC/WPAD 配置，未下载或执行脚本')
    if clock()>deadline:return {'status':'DISCOVERY_TIMEOUT','candidates':[],'warnings':warnings}
    return {'status':status,'candidates':ready,'warnings':warnings,
            'running_client_count':len(evidence.get('processes',[])),
            'target_access':'UNVERIFIED'}

def choose(discovery,requested='AUTO',preferred=None):
    from .network_readiness import NetworkNotReady
    choices=discovery.get('candidates',[])
    if requested=='SOCKS5_REQUIRED':choices=[c for c in choices if c['protocol'] in {'SOCKS5','MIXED'}]
    if preferred:
        choices=[{**c,'host':preferred['host']} for c in choices if preferred['host'] in c.get('host_aliases',[c['host']]) and c['port']==preferred['port']]
    if discovery.get('status') in {'TOO_MANY_CANDIDATES','DISCOVERY_TIMEOUT'}:choices=[]
    if len(choices)!=1:
        reason=('本机代理监听端口过多，且没有可核验的系统代理端口；请明确选择本机出口。' if discovery.get('status')=='TOO_MANY_CANDIDATES' else
                '本机代理协议探测超时；请检查客户端状态后重试。' if discovery.get('status')=='DISCOVERY_TIMEOUT' else
                '发现多个本机代理出口，请明确选择；未自动换出口。' if len(choices)>1 else
                '仅检测到 TUN VPN，沿用现有系统路由但目标未验证；未编造代理端口。' if discovery.get('status')=='TUN_ONLY' else
                '未检测到运行中的本机代理客户端；如已安装请先启动。未悄悄回退直连。' if not discovery.get('running_client_count') else
                '客户端已运行但未确认兼容代理端口；请检查监听协议与端口，未悄悄回退直连。')
        raise NetworkNotReady(reason,requested)
    item=choices[0]
    protocol='SOCKS5' if item['protocol']=='SOCKS5' or requested=='SOCKS5_REQUIRED' else 'HTTP'
    if preferred and preferred.get('protocol') not in (None,'AUTO'):
        desired=preferred['protocol']
        if (requested=='SOCKS5_REQUIRED' and desired!='SOCKS5') or desired not in {'SOCKS5','HTTP'} or item['protocol'] not in {desired,'MIXED'}:
            raise NetworkNotReady('所选协议与本机实际代理握手不一致，未使用。',requested)
        protocol=desired
    return {'preproxy_host':item['host'],'preproxy_port':item['port'],
            'preproxy_protocol':protocol,'preproxy_mode':requested,
            'network_probe_level':'LOCAL_PROXY_PROTOCOL_HANDSHAKE',
            'local_proxy_owner':{k:item[k] for k in ['pid','created','name']}}
