import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {terminalCollectionStop} from '../scripts/collection_checkpoint.mjs';

test('actual collection loop sends a login page to recovery before stale HTTP errors', async () => {
  const source = fs.readFileSync(new URL('../scripts/temu_flower_collect.mjs', import.meta.url), 'utf8');
  const start = source.indexOf('    const terminal = terminalCollectionStop(before, httpBlock);');
  const end = source.indexOf("    if (effectiveBlock !== 'HUMAN_VERIFICATION'", start);
  assert.ok(start > 0 && end > start);
  for (const status of [null, 403, 429]) {
    const snapshots = [];
    const context = vm.createContext({
      terminalCollectionStop, before: {url: 'https://www.temu.com/login.html'},
      httpBlock: status ? {status} : null, httpStatus: status, round: 3, finalStatus: null,
      confirmedLoginRedirect: () => true, appendEvent: async () => {},
      writeSnapshot: async (status, data) => snapshots.push({status, ...data}),
    });
    await vm.runInContext('(async () => { while (true) {\n' + source.slice(start, end) + '\nbreak; } })()', context);
    assert.equal(snapshots.length, 1);
    assert.equal(snapshots[0].status, 'STOPPED_LOGIN_REQUIRED');
    assert.equal(snapshots[0].automatic_retry, true);
    assert.equal(snapshots[0].manual_action_required, false);
    assert.equal(snapshots[0].resume_requires_session_rotation, true);
  }
});

test('verification and a normal search page do not trigger login rotation', () => {
  assert.equal(terminalCollectionStop({url: 'https://www.temu.com/login.html', text: 'Security verification'}, {status: 403}), null);
  assert.equal(terminalCollectionStop({url: 'https://www.temu.com/search_result.html', text: 'Sign in', visibleGoodsCount: 40}), null);
  const blocked = terminalCollectionStop({url: 'https://www.temu.com/search_result.html', text: 'Access denied'}, {status: 403});
  assert.equal(blocked.status, 'STOPPED_HTTP_403');
  assert.notEqual(blocked.automatic_retry, true);
});
