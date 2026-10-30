import test, {before, after} from 'node:test';
import assert from 'node:assert/strict';
import {chromium} from 'playwright';
import {companyLogin, loginOnPage, trustedGeekbiUrl} from '../scripts/geekbi_company_login.mjs';

let browser;
before(async () => {browser = await chromium.launch({headless: true});});
after(async () => {await browser?.close();});

async function fixture(html) {
  const page = await browser.newPage();
  await page.route('https://passport.geekbi.com/**', route => route.fulfill({contentType: 'text/html; charset=utf-8', body: html}));
  await page.goto('https://passport.geekbi.com/login');
  return page;
}
const credentials = {username: 'fixture-account', password: 'fixture-password'};

test('normal account form submits once and uses the explicit authorization control', async () => {
  const page = await fixture(`<form onsubmit="event.preventDefault();window.submissions=(window.submissions||0)+1;this.remove();document.querySelector('#authorize').hidden=false">
    <input placeholder="手机号"><input type="password"><button>登录</button></form>
    <button id="authorize" hidden onclick="window.authorized=true;this.remove()">确认授权</button>`);
  try {
    const result = await loginOnPage(page, credentials);
    assert.equal(result.status, 'AUTHORIZATION_SENT');
    assert.equal(result.diagnostics.submitted, true);
    assert.deepEqual(await page.evaluate(() => [window.submissions, window.authorized]), [1, true]);
  } finally {await page.close();}
});

test('the visible phone login mode opens only its form while email and duplicate controls stay hidden', async () => {
  const page = await fixture(`<button>微信登录</button>
    <button onclick="window.switches=(window.switches||0)+1;document.querySelector('#phone').hidden=false">手机号登录</button>
    <button>邮箱登录</button><button hidden>手机号登录</button>
    <form id="phone" hidden onsubmit="event.preventDefault();window.submissions=(window.submissions||0)+1;this.remove()">
      <input placeholder="手机号"><input type="password"><button>登录</button></form>
    <form id="email" hidden><input placeholder="邮箱"><input type="password"><button>登录</button></form>`);
  try {
    assert.equal(await page.locator('input[type="password"]').count(), 2);
    assert.equal(await page.locator('input[type="password"]:visible').count(), 0);
    const result = await loginOnPage(page, {...credentials, timeout_ms: 2000});
    assert.equal(result.status, 'AUTHORIZATION_SENT');
    assert.equal(result.diagnostics.submitted, true);
    assert.deepEqual(await page.evaluate(() => [window.switches, window.submissions]), [1, 1]);
    assert.equal(await page.locator('#email input[type="password"]').inputValue(), '');
  } finally {await page.close();}
});

test('captcha shown after switching to phone login stops before filling credentials', async () => {
  const page = await fixture(`<button onclick="document.querySelector('#phone').hidden=false">手机号登录</button>
    <form id="phone" hidden><div class="captcha">请拖动滑块</div>
      <input placeholder="手机号"><input type="password"><button>登录</button></form>`);
  try {
    const result = await loginOnPage(page, {...credentials, timeout_ms: 500});
    assert.equal(result.status, 'CAPTCHA_REQUIRED');
    assert.equal(result.diagnostics.submitted, false);
    assert.equal(await page.locator('input[type="password"]').inputValue(), '');
    assert.equal(await page.locator('input[placeholder="手机号"]').inputValue(), '');
  } finally {await page.close();}
});

test('the official phone form owns its password and submit even when the clipped email form is also visible', async () => {
  const page = await fixture(`<button>微信登录</button><button>手机号登录</button><button>邮箱登录</button>
    <div style="width:468px;overflow:hidden"><div style="display:flex;gap:18px">
      <form class="arco-form form-wrap" style="min-width:450px" onsubmit="event.preventDefault();window.phoneSubmissions=(window.phoneSubmissions||0)+1;this.remove()">
        <input placeholder="请输入手机号"><input type="password"><button>登录</button></form>
      <form id="email" class="arco-form form-wrap" style="min-width:450px" onsubmit="event.preventDefault();window.emailSubmissions=(window.emailSubmissions||0)+1">
        <input placeholder="请输入邮箱"><input type="password"><button>登录</button></form>
    </div></div>`);
  try {
    assert.equal(await page.locator('input[type="password"]:visible').count(), 2);
    const result = await loginOnPage(page, {...credentials, timeout_ms: 2000});
    assert.equal(result.status, 'AUTHORIZATION_SENT');
    assert.equal(result.diagnostics.submitted, true);
    assert.deepEqual(await page.evaluate(() => [window.phoneSubmissions, window.emailSubmissions || 0]), [1, 0]);
    assert.equal(await page.locator('#email input[type="password"]').inputValue(), '');
    assert.equal(await page.locator('#email input[placeholder="请输入邮箱"]').inputValue(), '');
  } finally {await page.close();}
});

test('two phone forms remain ambiguous and neither receives credentials', async () => {
  const page = await fixture(`<button>手机号登录</button>
    <form><input placeholder="请输入手机号"><input type="password"><button>登录</button></form>
    <form><input placeholder="请输入手机号"><input type="password"><button>登录</button></form>`);
  try {
    const result = await loginOnPage(page, {...credentials, timeout_ms: 2000});
    assert.equal(result.status, 'FORM_UNAVAILABLE');
    assert.equal(result.diagnostics.submitted, false);
    assert.deepEqual(await page.locator('input').evaluateAll(inputs => inputs.map(input => input.value)), ['', '', '', '']);
  } finally {await page.close();}
});

test('visible captcha stops before any credentials are filled or submitted', async () => {
  const page = await fixture('<div class="captcha">请拖动滑块</div><input placeholder="手机号"><input type="password"><button>登录</button>');
  try {
    assert.equal((await loginOnPage(page, {...credentials, timeout_ms: 100})).status, 'CAPTCHA_REQUIRED');
    assert.equal(await page.locator('input[type=password]').inputValue(), '');
  } finally {await page.close();}
});

for (const solved of [true, false]) {
  test(`a submitted captcha keeps the same window until ${solved ? 'the user completes it' : 'the short budget expires'}`, async t => {
    const page = await fixture(`<form id="phone" onsubmit="event.preventDefault();window.submissions=(window.submissions||0)+1;document.querySelector('#challenge').hidden=false">
      <input placeholder="请输入手机号"><input type="password"><button>登录</button></form>
      <div id="challenge" class="captcha" hidden>请拖动滑块</div>
      <button id="authorize" hidden onclick="window.authorized=true;this.remove()">确认授权</button>`);
    let closed = false, submissions = 0, authorized = false;
    const startedAt = Date.now();
    let elapsed = 0;
    t.mock.method(Date, 'now', () => startedAt + elapsed);
    t.mock.method(page, 'goto', async () => ({status: () => 200}));
    t.mock.method(chromium, 'launchPersistentContext', async () => ({
      setDefaultTimeout: timeout => page.setDefaultTimeout(timeout), pages: () => [page],
      close: async () => {
        [submissions, authorized] = await page.evaluate(() => [window.submissions || 0, !!window.authorized]);
        closed = true;
        await page.close();
      },
    }));
    try {
      const pending = companyLogin({...credentials, jump_url: 'https://passport.geekbi.com/login',
        profile: 'fixture-profile', timeout_ms: solved ? 3000 : 1500});
      await page.waitForFunction(() => window.submissions === 1);
      await page.waitForTimeout(700);
      assert.equal(closed, false);
      elapsed = solved ? 700 : 1500;
      if (solved) await page.evaluate(() => {
        document.querySelector('#challenge').remove();
        document.querySelector('#phone').remove();
        document.querySelector('#authorize').hidden = false;
      }); // Fixture-only simulation of the user completing the challenge.
      const result = await pending;
      assert.equal(result.status, solved ? 'AUTHORIZATION_SENT' : 'CAPTCHA_REQUIRED');
      assert.equal(result.diagnostics.submitted, true);
      assert.equal(closed, true);
      assert.equal(submissions, 1);
      assert.equal(authorized, solved);
    } finally {if (!page.isClosed()) await page.close();}
  });
}

test('account rejection leaves the page pending after one submission', async () => {
  const page = await fixture(`<form onsubmit="event.preventDefault();window.submissions=(window.submissions||0)+1;document.querySelector('#error').textContent='账号或密码错误'">
    <input placeholder="手机号"><input type="password"><button>登录</button></form><p id="error"></p>`);
  try {
    const result = await loginOnPage(page, credentials);
    assert.equal(result.status, 'LOGIN_REJECTED');
    assert.equal(result.diagnostics.submitted, true);
    assert.equal(await page.evaluate(() => window.submissions), 1);
  } finally {await page.close();}
});

test('an ambiguous account form is never submitted', async () => {
  const page = await fixture('<input placeholder="手机号"><input placeholder="邮箱"><input type="password"><button>登录</button>');
  try {
    assert.equal((await loginOnPage(page, credentials)).status, 'FORM_UNAVAILABLE');
    assert.equal(await page.locator('input[type=password]').inputValue(), '');
  } finally {await page.close();}
});

for (const [name, html] of [['empty page', '<body></body>'],
  ['QR-only page', '<body><img alt="扫码登录"><p>请使用微信扫码登录</p></body>']]) {
  test(`${name} stays unsubmitted when no normal form appears`, async () => {
    const page = await fixture(html);
    try {
      const result = await loginOnPage(page, {...credentials, timeout_ms: 100});
      assert.deepEqual(result, {status: 'FORM_UNAVAILABLE', diagnostics: {
        password_count: 0, account_count: 0, submit_count: 0, submitted: false, callback: false,
      }});
      assert.equal(JSON.stringify(result).includes(credentials.password), false);
      assert.equal(JSON.stringify(result).includes('扫码'), false);
    } finally {await page.close();}
  });
}

test('a normally delayed form is awaited and submitted once', async () => {
  const page = await fixture(`<script>setTimeout(() => {
    document.body.innerHTML='<form onsubmit="event.preventDefault();window.submissions=(window.submissions||0)+1;this.remove()"><input placeholder="手机号"><input type="password"><button>登录</button></form>';
  }, 100);</script>`);
  try {
    const result = await loginOnPage(page, {...credentials, timeout_ms: 2000});
    assert.equal(result.status, 'AUTHORIZATION_SENT');
    assert.equal(result.diagnostics.submitted, true);
    assert.equal(await page.evaluate(() => window.submissions), 1);
  } finally {await page.close();}
});

test('an explicit authorization button is clicked once without a password form', async () => {
  const page = await fixture('<button onclick="window.authorizations=(window.authorizations||0)+1;this.remove()">确认授权</button>');
  try {
    const result = await loginOnPage(page, credentials);
    assert.equal(result.status, 'AUTHORIZATION_SENT');
    assert.equal(result.diagnostics.submitted, true);
    assert.equal(await page.evaluate(() => window.authorizations), 1);
  } finally {await page.close();}
});

test('credentials can only be submitted on GeekBI HTTPS pages', () => {
  assert.equal(trustedGeekbiUrl('https://passport.geekbi.com/login'), true);
  for (const address of ['http://passport.geekbi.com/login', 'https://geekbi.com.example/login',
    'https://geekbi.com@evil.example/login', 'https://passport.geekbi.com:8443/login'])
    assert.equal(trustedGeekbiUrl(address), false);
});

for (const [navigation, status] of [[403, 'LOGIN_HTTP_403'], [429, 'LOGIN_HTTP_429'], ['network', 'LOGIN_NETWORK_FAILED']]) {
  test(`navigation ${navigation} stops before any account form action`, async t => {
    let closed = false;
    const page = {goto: async () => {
      if (navigation === 'network') throw new Error('PRIVATE_NAVIGATION_DATA');
      return {status: () => navigation};
    }};
    t.mock.method(chromium, 'launchPersistentContext', async (_profile, options) => {
      assert.equal(options.headless, false);
      return {setDefaultTimeout() {}, pages: () => [page], close: async () => {closed = true;}};
    });
    const result = await companyLogin({...credentials, jump_url: 'https://passport.geekbi.com/login',
      profile: 'fixture-profile', timeout_ms: 1000});
    assert.deepEqual(result, {status, diagnostics: {submitted: false, callback: false}});
    assert.equal(closed, true);
    assert.equal(JSON.stringify(result).includes('PRIVATE_NAVIGATION_DATA'), false);
  });
}
