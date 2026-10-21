import test from 'node:test';
import assert from 'node:assert/strict';
import {amazonBrowserRoute} from '../scripts/amazon_browser_route.mjs';

test('DIRECT disables Chromium system proxy even with an inherited Amazon proxy', () => {
  const environment = {AMAZON_BROWSER_ROUTE_MODE: 'DIRECT', AMAZON_BROWSER_PROXY: 'http://127.0.0.1:7890',
    HTTPS_PROXY: 'http://127.0.0.1:7890', MODEL_API_KEY: 'fixture-key'};
  const before = {...environment};
  const selected = amazonBrowserRoute(environment);
  assert.deepEqual(selected.launchOptions, {args: ['--no-proxy-server']});
  assert.equal(Object.hasOwn(selected.launchOptions, 'proxy'), false);
  assert.deepEqual(environment, before);
  assert.deepEqual(selected.evidence, {mode: 'DIRECT', transport: 'BROWSER_DIRECT', egress_verified: false});
  assert.equal(JSON.stringify(selected.evidence).includes('fixture-'), false);
});

test('AUTO preserves the configured local proxy and never reports verified egress', () => {
  const selected = amazonBrowserRoute({AMAZON_BROWSER_PROXY: 'http://127.0.0.1:7890'});
  assert.deepEqual(selected.launchOptions, {proxy: {server: 'http://127.0.0.1:7890'}});
  assert.deepEqual(selected.evidence, {mode: 'AUTO', transport: 'CONFIGURED_PROXY', egress_verified: false});
  assert.deepEqual(amazonBrowserRoute({}).launchOptions, {});
});

test('invalid route is rejected without reflecting its value into diagnostics', () => {
  assert.throws(() => amazonBrowserRoute({AMAZON_BROWSER_ROUTE_MODE: 'https://fixture-sensitive.invalid'}),
    {message: 'Amazon browser route must be AUTO or DIRECT'});
});
