// Company credentials arrive only on stdin. Do not log page text or exceptions.
import path from 'node:path';
import {pathToFileURL} from 'node:url';

export function trustedGeekbiUrl(value) {
  const address = new URL(value);
  return address.protocol === 'https:' && !address.username && !address.password
    && (!address.port || address.port === '443')
    && (address.hostname === 'geekbi.com' || address.hostname.endsWith('.geekbi.com'));
}

export async function loginOnPage(page, request) {
  const diagnostics = {password_count: 0, account_count: 0, submit_count: 0, submitted: false, callback: false};
  const accountSelector = 'input[type="tel"]:visible,input[autocomplete="username"]:visible,input[placeholder*="手机号"]:visible,input[placeholder*="手机号码"]:visible,input[placeholder*="账号"]:visible,input[placeholder*="邮箱"]:visible,input[name="username"]:visible';
  const submitName = /^(?:登录|登\s+录|立即登录|登录并授权|Log in|Sign in)$/i;
  const authorizeName = /^(?:确认授权|同意授权|授权|Authorize|Allow)$/i;
  let scope = page;
  const result = async status => {
    diagnostics.password_count = await scope.locator('input[type="password"]:visible').count();
    diagnostics.account_count = await scope.locator(accountSelector).count();
    diagnostics.submit_count = await scope.getByRole('button', {name: submitName}).count()
      + await page.getByRole('button', {name: authorizeName}).count();
    return {status, diagnostics};
  };
  const deadline = Date.now() + Math.min(request.timeout_ms ?? 20000, 60000);
  // These semantic selectors are intentionally isolated here. A site layout
  // change must leave the authorization pending, never guess at unrelated forms.
  const stopReason = async () => {
    while (true) {
      if (!trustedGeekbiUrl(page.url())) return 'LOGIN_FAILED';
      const security = page.locator('iframe[src*="captcha"],iframe[src*="geetest"],iframe[src*="recaptcha"],[id*="captcha"],[class*="captcha"],[class*="geetest"]');
      let captcha = false;
      for (const item of await security.all()) if (await item.isVisible()) captcha = true;
      const text = await page.locator('body').innerText();
      captcha ||= /人机验证|请(?:完成|通过).{0,8}(?:验证|滑块)|请拖动|拖动滑块|安全(?:警告|验证|风险)|验证码(?:错误|不正确)/i.test(text);
      if (captcha) {
        const remaining = deadline - Date.now();
        if (remaining <= 0) return 'CAPTCHA_REQUIRED';
        await page.waitForTimeout(Math.min(500, remaining));
        continue; // Keep this same window for the user; never act on the challenge.
      }
      if (/(?:账号|账户)(?:已|被)?(?:禁用|冻结|锁定)|(?:账号|账户|密码)(?:或密码)?(?:不正确|错误)|登录失败|用户名或密码/i.test(text)) return 'LOGIN_REJECTED';
      return '';
    }
  };
  let reason = await stopReason();
  if (reason) return result(reason);

  const mode = page.getByText(/^(?:手机号登录|账号密码登录|密码登录)$/).filter({visible: true});
  const pagePasswords = page.locator('input[type="password"]:visible');
  const phoneForm = page.locator('form').filter({has: page.locator('input[placeholder="请输入手机号"]:visible')});
  const authorize = page.getByRole('button', {name: authorizeName});
  let password, username, submit, modeSelected = false;
  while (true) {
    reason = await stopReason();
    if (reason) return result(reason);
    if (!modeSelected && await mode.count() === 1 && await mode.isVisible()) {
      await mode.click();
      modeSelected = true;
    }
    const form = await phoneForm.count() === 1 ? phoneForm : pagePasswords.locator('xpath=ancestor::form[1]');
    scope = await form.count() === 1 ? form : page;
    password = scope.locator('input[type="password"]:visible');
    diagnostics.password_count = await password.count();
    username = scope.locator(accountSelector);
    submit = scope.getByRole('button', {name: submitName});
    diagnostics.account_count = await username.count();
    diagnostics.submit_count = await submit.count() + await authorize.count();
    if (diagnostics.password_count > 1 || diagnostics.account_count > 1 || diagnostics.submit_count > 1)
      return result('FORM_UNAVAILABLE');
    if (diagnostics.password_count === 1 && diagnostics.account_count === 1 && await submit.count() === 1
        || diagnostics.password_count === 0 && await authorize.count() === 1) break;
    if (Date.now() >= deadline) return result('FORM_UNAVAILABLE');
    await page.waitForTimeout(Math.min(250, deadline - Date.now()));
  }
  if (diagnostics.password_count === 1) {
    reason = await stopReason();
    if (reason) return result(reason);
    await username.fill(request.username);
    await password.fill(request.password);
    await submit.click(); // Exactly one credential submission for this challenge.
    diagnostics.submitted = true;
    while (await password.count() !== 0) {
      if (Date.now() >= deadline) return result('LOGIN_FAILED');
      await page.waitForTimeout(Math.min(500, deadline - Date.now()));
      reason = await stopReason();
      if (reason) return result(reason);
    }
  }

  reason = await stopReason();
  if (reason) return result(reason);
  if (await authorize.count() === 1 && await authorize.isVisible()) {
    await authorize.click();
    diagnostics.submitted = true;
    await page.waitForLoadState('domcontentloaded');
  }
  // The Python SDK must obtain a real token before reporting authorization.
  return result(diagnostics.submitted ? 'AUTHORIZATION_SENT' : 'FORM_UNAVAILABLE');
}

export async function companyLogin(request) {
  if (!trustedGeekbiUrl(request.jump_url) || typeof request.username !== 'string' || !request.username
      || typeof request.password !== 'string' || !request.password
      || typeof request.profile !== 'string' || !request.profile
      || !Number.isInteger(request.timeout_ms) || request.timeout_ms < 1 || request.timeout_ms > 60000)
    return {status: 'LOGIN_FAILED'};
  const deadline = Date.now() + request.timeout_ms;
  const {chromium} = await import('playwright');
  let context;
  try {
    context = await chromium.launchPersistentContext(request.profile, {headless: false});
    context.setDefaultTimeout(Math.min(15000, request.timeout_ms));
    const page = context.pages()[0] || await context.newPage();
    let response;
    try {
      response = await page.goto(request.jump_url, {waitUntil: 'domcontentloaded', timeout: Math.max(1, deadline - Date.now())});
    } catch {
      return {status: 'LOGIN_NETWORK_FAILED', diagnostics: {submitted: false, callback: false}};
    }
    if (response?.status() === 403 || response?.status() === 429)
      return {status: response.status() === 403 ? 'LOGIN_HTTP_403' : 'LOGIN_HTTP_429',
        diagnostics: {submitted: false, callback: false}};
    const remaining = deadline - Date.now();
    if (remaining <= 0) return {status: 'LOGIN_FAILED', diagnostics: {submitted: false, callback: false}};
    return await loginOnPage(page, {...request, timeout_ms: remaining});
  } finally {
    if (context) await context.close();
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  let result;
  try {
    let input = '';
    for await (const chunk of process.stdin) input += chunk;
    const request = JSON.parse(input);
    input = '';
    result = await companyLogin(request);
    request.password = '';
    request.username = '';
  } catch {
    result = {status: 'LOGIN_FAILED'};
  }
  process.stdout.write(JSON.stringify(result));
}
