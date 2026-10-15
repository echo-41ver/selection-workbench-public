"""Detect native client gates without interacting with verification widgets."""


class AgentActionRequired(RuntimeError):
    def __init__(self,agent_id,code,message):
        super().__init__(message)
        self.agent_id=agent_id;self.code=code


def require_doubao_ready(cdp):
    blocked=cdp.evaluate("""(()=>{const visible=e=>e.getClientRects().length&&
      getComputedStyle(e).visibility!=='hidden'&&getComputedStyle(e).display!=='none';
      return [...document.querySelectorAll('#vc_captcha_wrapper,#vc_captcha_box,[data-testid="captcha"],iframe[src*="captcha"]')].some(visible);})()""")
    if blocked is True:
        raise AgentActionRequired('doubao','HUMAN_VERIFICATION_REQUIRED','豆包出现验证码，请本人完成验证后继续；已保存断点')
