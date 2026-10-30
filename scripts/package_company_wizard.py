"""Human-operated full internal bundle wizard. Never run during release builds."""
import argparse,json,sys,zipfile,os,uuid,shutil,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from selection_automation.company_bundle import normalize, portable

def write_bundle(base,destination,configuration):
    config=portable(configuration);base=Path(base);destination=Path(destination)
    if destination.exists() or destination.resolve()==base.resolve():raise ValueError('Output must be a new internal package file')
    temporary=destination.with_name(destination.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        with zipfile.ZipFile(base) as source,zipfile.ZipFile(temporary,'w',zipfile.ZIP_DEFLATED) as out:
            info=json.loads(source.read('BUILD_INFO.json'))
            if info.get('channel')!='internal' or info.get('contains_company_credentials') or 'company.local.json' in source.namelist():raise ValueError('Use a clean internal base package')
            for entry in source.infolist():
                if entry.filename!='BUILD_INFO.json':
                    with source.open(entry) as data,out.open(entry,'w') as target:shutil.copyfileobj(data,target)
            info.update(contains_company_credentials=True,company_config_schema='company-bundle-v2',unattended_company_import=True)
            out.writestr('BUILD_INFO.json',json.dumps(info,ensure_ascii=False,indent=2))
            out.writestr('company.local.json',json.dumps(config,ensure_ascii=False,indent=2))
        with zipfile.ZipFile(temporary) as archive:
            if archive.testzip() is not None:raise ValueError('Package validation failed')
        # Hard-link publication refuses an existing target even if another process creates it mid-write.
        os.link(temporary,destination)
    finally:temporary.unlink(missing_ok=True)
    return destination

def wizard(root,base,output,company_json=None):
    import tkinter as tk
    from tkinter import messagebox,filedialog
    window=tk.Tk();window.withdraw()
    if not messagebox.askyesno('本人确认读取本机配置','将读取您选择的本机工作台中已保存的 AdsPower、代理、D1、DeepSeek 和 GeekBI 配置（含接口授权凭证及有效期），并生成含真实密钥的内部包。\n只交给获准使用者，禁止上传公开 GitHub。原配置和现有包不改。\n\n是否由您本人继续？',default='no',parent=window):return
    from selection_automation.secrets import load
    company={};model={};decision={};captcha={};root=Path(root)
    for name in ('company','deepseek','liquid_d1','twocaptcha'):
        path=root/'runtime/secrets'/f'{name}.dpapi'
        if path.exists():
            try:value=load(path)
            except Exception:
                messagebox.showerror('无法读取本机配置','保存的配置无法由当前 Windows 用户解密。请切回原用户，或在表单中重新填写。',parent=window);value={}
            if name=='company':company=value
            elif name=='deepseek':model=value
            elif name=='liquid_d1':decision=value
            else:captcha=value
    installed_username=(company.get('geekbi') or {}).get('username','').strip()
    if company_json:
        try:company=json.loads(Path(company_json).read_text(encoding='utf-8-sig'))
        except Exception:
            messagebox.showerror('配置文件无效','无法读取所选公司 JSON；未生成包或修改原配置。',parent=window);window.destroy();return
    model=company.get('deepseek') or model
    decision=company.get('liquid_d1') or decision
    captcha=company.get('twocaptcha') or captcha
    proxy=company.get('proxy',{})
    geekbi=company.get('geekbi') or {}
    fields=[('AdsPower API 地址','adspower_api_url',company.get('adspower_api_url','http://127.0.0.1:50325'),False),('AdsPower API Key','adspower_api_key',company.get('adspower_api_key',''),True),('代理服务器','host',proxy.get('host','proxy.proxyshare.com'),False),('代理端口','port',proxy.get('port',5959),False),('代理账号（含 _session-）','username',proxy.get('username',''),False),('代理密码','password',proxy.get('password',''),True),('DeepSeek 地址','base_url',model.get('base_url','https://api.deepseek.com'),False),('DeepSeek 模型','model',model.get('model','deepseek-flash'),False),('DeepSeek API Key','key',model.get('key',''),True)]
    window.title('生成完整内部一键部署包');window.deiconify();entries={}
    fields.append(('D1 API Key','d1_key',decision.get('key',''),True))
    fields.extend([('GeekBI 账号（可选）','geekbi_username',geekbi.get('username',''),False),('GeekBI 密码（可选）','geekbi_password',geekbi.get('password',''),True)])
    tk.Label(window,text='已保存配置自动填写；密钥仅在本机。缺项可在这里补充，无需手写 JSON。').grid(row=0,column=0,columnspan=2,padx=12,pady=10)
    for n,(label,key,value,secret) in enumerate(fields,1):
        tk.Label(window,text=label).grid(row=n,column=0,sticky='w',padx=12,pady=4)
        entry=tk.Entry(window,width=58,show='*' if secret else '');entry.insert(0,str(value));entry.grid(row=n,column=1,padx=12,pady=4);entries[key]=entry
    def create():
        try:
            values={key:entry.get() for key,entry in entries.items()}
            try:port=int(values['port'])
            except ValueError:raise ValueError('代理端口必须是 1–65535 的整数')
            payload={'adspower_api_url':values['adspower_api_url'],'adspower_api_key':values['adspower_api_key'],'proxy':{'host':values['host'],'port':port,'username':values['username'],'password':values['password']},'preproxy_candidates':company.get('preproxy_candidates',[]),'deepseek':{'base_url':values['base_url'],'model':values['model'],'key':values['key']}}
            if 'preproxy_mode' in company:payload['preproxy_mode']=company['preproxy_mode']
            if 'local_proxy_endpoint' in company:payload['local_proxy_endpoint']=company['local_proxy_endpoint']
            if values['d1_key'].strip():payload['liquid_d1']={'key':values['d1_key'],'base_url':'https://api.liquid.ai/decisions/v1/systemone','model':'d1'}
            if captcha.get('key'):payload['twocaptcha']=captcha
            if values['geekbi_username'].strip() or values['geekbi_password'].strip():
                payload['geekbi']={'username':values['geekbi_username'],'password':values['geekbi_password']}
                if values['geekbi_username'].strip()==geekbi.get('username','').strip():
                    candidates=[(geekbi,'access_token','expires_at')]
                    if installed_username and installed_username==geekbi.get('username','').strip():
                        try:
                            saved=json.loads((root/'runtime/market-auth/agent-auth.json').read_text(encoding='utf-8-sig')).get('servers',{}).get('https://openapi.geekbi.com',{})
                        except (OSError,ValueError,AttributeError):saved={}
                        candidates.append((saved,'accessToken','accessTokenExpiresAt'))
                    for candidate,token_key,expiry_key in candidates:
                        if not isinstance(candidate,dict):continue
                        token=candidate.get(token_key);expiry=candidate.get(expiry_key)
                        if (isinstance(token,str) and token.strip() and not any(ord(c)<32 for c in token)
                                and type(expiry) is int and expiry>time.time()):
                            payload['geekbi'].update(access_token=token,expires_at=expiry)
                            break
            normalize(payload,require_model=True)
            if not messagebox.askyesno('最终确认装入真实密钥','新包将含 AdsPower、代理、D1、DeepSeek 真实密钥，以及已填写的 GeekBI 账号密码、接口授权凭证及有效期。\n仅供获准使用者使用，禁止公开上传。\n\n是否确认生成？',default='no',parent=window):return
            created=write_bundle(base,output,payload)
            messagebox.showinfo('完成','完整内部包已生成：\n'+str(created)+'\n\n解压后双击 deploy.cmd，并在其电脑确认加密导入。',parent=window);window.destroy()
        except ValueError as error:messagebox.showerror('配置需完善',str(error),parent=window)
        except Exception:messagebox.showerror('装包失败','未改动原包或原配置。请检查输出目录权限与可用空间。',parent=window)
    tk.Button(window,text='确认生成完整内部包',command=create).grid(row=len(fields)+1,column=0,columnspan=2,pady=14)
    window.mainloop()
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--base',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--company-json',type=Path);a=p.parse_args();wizard(a.root,a.base,a.output,a.company_json)
