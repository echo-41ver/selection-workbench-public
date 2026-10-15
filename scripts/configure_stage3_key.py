"""Local masked key entry; no credentials in command arguments or terminal output."""
import argparse
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))


def configure(root):
    import tkinter as tk
    from tkinter import messagebox
    from selection_automation.stage3_credentials import save_key
    from selection_automation.stage3_provider import settings
    window=tk.Tk();window.title('保存 D1 Key · 选品工作台')
    tk.Label(window,text='请在本机填写当前 D1 API Key。\n保存后按当前 Windows 用户加密，供工作台及内部部署包使用。',justify='left').pack(padx=20,pady=14)
    entry=tk.Entry(window,width=66,show='*');entry.pack(padx=20,pady=6);entry.focus_set()
    state=tk.StringVar(value='Key 不会发送到聊天或写入程序源码。')
    tk.Label(window,textvariable=state).pack(padx=20,pady=6)
    def save():
        try:save_key(root,settings(),entry.get())
        except ValueError as exc:state.set(str(exc));return
        except Exception:state.set('本机加密保存失败，请检查目录权限。');return
        entry.delete(0,'end');messagebox.showinfo('已保存','D1 Key 已加密保存。',parent=window);window.destroy()
    tk.Button(window,text='保存 D1 Key',command=save).pack(pady=14)
    window.mainloop()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,default=ROOT)
    configure(parser.parse_args().root.resolve())
