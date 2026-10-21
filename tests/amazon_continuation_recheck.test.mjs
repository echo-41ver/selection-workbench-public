import test from 'node:test';
import assert from 'node:assert/strict';
import {runAmazonDetailQueue} from '../scripts/amazon_detail_queue.mjs';
import {completeDetail} from './fixtures/amazon_detail.mjs';

const card = (asin, rank) => ({asin, rank, title: asin, product_url: `https://www.amazon.com/dp/${asin}`});
const paused = (asin, rank, code = 'AMAZON_CONTINUATION_REQUIRED') => ({...card(asin, rank),
  detail_image_attempts: 8, detail_error: {code, retryable: false, message: 'fixture gate'}});
const queue = options => runAmazonDetailQueue({onCheckpoint: async () => {},
  wait: async () => assert.fail('Explicit recheck must not enter an automatic retry wave'), ...options});

test('explicit continuation rechecks only the exact paused navigation gate and retains other gates and successes', async () => {
  const records = [paused('CONTINUE', 1), paused('CAPTCHA', 2, 'AMAZON_VERIFICATION_REQUIRED'),
    paused('LOGIN', 3, 'AMAZON_LOGIN_REQUIRED'), paused('HTTP403', 4, 'AMAZON_HTTP_403'),
    paused('HTTP429', 5, 'AMAZON_HTTP_429'), completeDetail(card('DONE', 6))];
  const before = structuredClone(records), calls = [];
  const result = await queue({records, recheckContinuation: true,
    fetchDetail: async (row, attempt) => {calls.push([row.asin, attempt]); return completeDetail(row);}});
  assert.deepEqual(calls, [['CONTINUE', 9]]);
  assert.deepEqual(result.records.map(row => [row.asin, row.rank]), records.map(row => [row.asin, row.rank]));
  assert.deepEqual(result.records.slice(1), before.slice(1));
  assert.deepEqual(records, before);
  assert.deepEqual(result.queue.manual_action_asins, ['CAPTCHA', 'LOGIN', 'HTTP403', 'HTTP429']);
  assert.deepEqual(result.queue.continuation_recheck_asins, ['CONTINUE']);
});

test('an unresolved explicit recheck makes one request and pauses again without reopening on background restart', async () => {
  let requests = 0;
  const result = await queue({records: [paused('CONTINUE', 1)], recheckContinuation: true,
    fetchDetail: async row => {requests++; return row;}});
  assert.equal(requests, 1);
  assert.equal(result.queue.status, 'MANUAL_ACTION_REQUIRED');
  assert.equal(result.records[0].detail_image_attempts, 9);
  assert.equal(result.records[0].detail_error.retryable, false);
  const resumed = await queue({records: result.records, fetchDetail: async () => assert.fail('No new user authorization')});
  assert.equal(resumed.requests, 0);
  assert.equal(resumed.records[0].detail_image_attempts, 9);
});

test('a transport error during explicit continuation recheck is bounded and keeps its cause', async () => {
  const result = await queue({records: [paused('CONTINUE', 1)], recheckContinuation: true,
    fetchDetail: async () => {throw new Error('fixture network timeout');}});
  assert.equal(result.requests, 1);
  assert.equal(result.queue.status, 'MANUAL_ACTION_REQUIRED');
  assert.equal(result.records[0].detail_error.code, 'AMAZON_CONTINUATION_REQUIRED');
  assert.equal(result.records[0].detail_error.retryable, false);
  assert.equal(result.records[0].detail_error.recheck_error.code, 'DETAIL_REQUEST_FAILED');
});

test('an interrupted authorized request retains its attempt reservation and remains paused without a new flag', async () => {
  let saved;
  await assert.rejects(queue({records: [paused('CONTINUE', 1)], recheckContinuation: true,
    onCheckpoint: async state => {
      if(state.queue.items[0].status === 'IN_PROGRESS') {
        saved = structuredClone(state.records);
        throw new Error('fixture process interruption');
      }
    }, fetchDetail: async () => assert.fail('Interruption occurs before fetch')}), /process interruption/);
  assert.equal(saved[0].detail_image_attempts, 9);
  assert.equal(saved[0].detail_error.retryable, false);
  const result = await queue({records: saved, fetchDetail: async () => assert.fail('Background restart stays paused')});
  assert.equal(result.requests, 0);
  assert.equal(result.queue.status, 'MANUAL_ACTION_REQUIRED');
});
