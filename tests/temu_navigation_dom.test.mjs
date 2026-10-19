import {inspectHumanVerification} from '../scripts/temu_verification.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { inspectSearchEnd } from '../scripts/collection_checkpoint.mjs';
import { inspectSearchReadiness } from '../scripts/temu_search_readiness.mjs';
import {parseProductCardMetrics} from '../scripts/product_card_metrics.mjs';

const source = fs.readFileSync(new URL('../scripts/temu_flower_collect.mjs', import.meta.url), 'utf8');
function embedded(name) {
  const start = source.indexOf(`const ${name} = \``) + `const ${name} = `.length;
  const end = source.indexOf('`;', start) + 1;
  assert.ok(start > name.length && end > start);
  return vm.runInNewContext(source.slice(start, end),{parseProductCardMetrics,inspectSearchReadiness,inspectSearchEnd,inspectHumanVerification});
}
function context(root = null) {
  return vm.createContext({
    document: { documentElement: root, body: null, images: [], querySelectorAll: () => [],
      documentURI: 'https://www.temu.com/', title: '', readyState: 'loading' },
    window: {}, location: { href: 'https://www.temu.com/', pathname: '/' },
    scrollY: 0, innerHeight: 100, innerWidth: 100, URL,
    MutationObserver: class { constructor() { throw new Error('observer created before document ready'); } },
  });
}
test('full product scan accepts a temporarily absent navigation root', () => {
  const result = vm.runInContext(`(function(){${embedded('extractScript')}})({knownIds:[],repairIds:[]})`, context());
  assert.equal(result.rows.length, 0);
  assert.equal(result.scrollHeight, 0);
});
test('light page reads preserve normal height and tolerate the navigation gap', async () => {
  const start = source.indexOf('async function lightPageState()');
  const end = source.indexOf('\nasync function visibleScrollToBottom', start);
  const make = new Function('execute', 'inspectSearchEnd','inspectSearchReadiness','inspectHumanVerification', source.slice(start, end) + ';return lightPageState;');
  for (const root of [null, { scrollHeight: 1234 }]) {
    const ctx = context(root);
    const result = await make(script => vm.runInContext(`(function(){${script}})()`, ctx), inspectSearchEnd,inspectSearchReadiness,inspectHumanVerification)();
    assert.equal(result.scrollHeight, root ? 1234 : 0);
    assert.equal(result.visibleGoodsCount, 0);
  }
});
test('image observer is deferred until the document root exists', () => {
  const result = vm.runInContext(`(function(){${embedded('neutralizeImagesScript')}})()`, context());
  assert.equal(result.active, false);
  assert.equal(result.reason, 'DOCUMENT_NOT_READY');
});
