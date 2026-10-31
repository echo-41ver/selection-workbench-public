"""Initial TEMU wait regression coverage with fake pages and a fake clock only."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js is required")


def run_node(code):
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", code], cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8", timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_initial_wait_policy_pauses_human_time_without_resetting_budget():
    result = run_node("""
      import {decideInitialProductsWait as decide} from './scripts/temu_initial_wait_policy.mjs';
      const page = {url:'https://www.temu.com/search_result.html?search_key=towels'};
      let previous = null;
      const observe = (nowMs, text = '', productsReady = false) => {
        const result = decide({pageState:{...page,text,loginFormVisible:text==='sign in'},previous,nowMs,productsReady});
        previous = result.clock;
        return result;
      };
      const results = [observe(0), observe(170000), observe(171000,'verify you are human'),
        observe(900000,'verify you are human'), observe(901000,'sign in'),
        observe(1500000,'sign in'), observe(1501000), observe(1510999), observe(1511000)];
      console.log(JSON.stringify(results));
    """)
    assert result[1]["clock"]["activeWaitMs"] == 170000
    assert [item["status"] for item in result[2:6]] == [
        "WAITING_CAPTCHA", "WAITING_CAPTCHA", "WAITING_LOGIN", "WAITING_LOGIN",
    ]
    assert all(not item["stop"] for item in result[:-1])
    assert result[-1]["clock"]["activeWaitMs"] == 180000
    assert result[-1]["status"] == "STOPPED_INITIAL_PRODUCTS_TIMEOUT"


def test_initial_wait_policy_ready_and_explicit_failures():
    result = run_node("""
      import {decideInitialProductsWait as decide} from './scripts/temu_initial_wait_policy.mjs';
      const previous = {observedAtMs:0,activeWaitMs:0,manual:false};
      const options = {previous,nowMs:5000,timeoutMs:5000};
      console.log(JSON.stringify([
        decide({...options,productsReady:true}),
        decide({...options,pageState:{documentURI:'about:neterror?e=proxyConnectFailure'},productsReady:true}),
        decide({...options,pageState:{text:'Access Denied'}}),
        decide({...options,nowMs:4999}), decide(options)
      ]));
    """)
    assert [item["status"] for item in result] == [
        "INITIAL_PRODUCTS_READY", "STOPPED_PROXY_OR_NETWORK_FAILURE", "STOPPED_ACCESS_DENIED",
        "WAITING_INITIAL_PRODUCTS", "STOPPED_INITIAL_PRODUCTS_TIMEOUT",
    ]


HARNESS = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import {decideInitialProductsWait,decideEmptyQueryResults} from './scripts/temu_initial_wait_policy.mjs';
import * as bootstrapPolicy from './scripts/temu_search_bootstrap_policy.mjs';
import {terminalCollectionStop} from './scripts/collection_checkpoint.mjs';
import {classifyPageState as classify} from './scripts/temu_flower_recovery_policy.mjs';
import {supersededByVerification} from './scripts/temu_network_evidence.mjs';
import {observeHttpPageTransition} from './scripts/temu_http_transition.mjs';
const source = fs.readFileSync('./scripts/temu_flower_collect.mjs','utf8');
const guard = source.slice(source.indexOf('class InitialProductsStopped'), source.indexOf('async function navigateConfirmed'));
const loops = source.slice(source.indexOf('async function waitForStableEmptySearch'), source.indexOf('const bidi = await startBidi'));
const initial = source.slice(source.indexOf('async function waitForInitialProducts()'), source.indexOf('try { await waitForInitialProducts(); }'));
const driver = source.slice(source.indexOf('try { await waitForInitialProducts(); }'));
let tick = 0, calls = 0, reloads = 0, cleanups = 0, callsAtCleanup = null, exitCode = null;
const manualExit = {};
const snapshots = [], events = [];
const empty = {url:'https://www.temu.com/search_result.html?search_key=towels',visibleGoodsCount:0,text:''};
const searchBox = {...empty,url:'https://www.temu.com/search_result.html',readyState:'complete',
  searchReady:true,searchEnabled:true,inputRect:{x:10,y:10,width:200,height:40}};
const failure = testCase.failure === 'network' ? {...empty,documentURI:'about:neterror?e=proxyConnectFailure'}
  : testCase.failure === 'denied' ? {...empty,text:'Access Denied'} : empty;
const page = async () => {
  calls++;
  if (testCase.disabled) return {...searchBox,searchEnabled:Boolean(testCase.recoverAfterReload && reloads)};
  // Allow the search box to stabilize first so the search-submission loop is exercised.
  if (testCase.entry === 'bootstrapThroughEmptySearch' && calls <= 4) return searchBox;
  if (testCase.manual && tick < 900000) return {...empty,text:'verify you are human'};
  return failure;
};
const context = vm.createContext({
  ...bootstrapPolicy, decideInitialProductsWait,decideEmptyQueryResults, classify, terminalCollectionStop,
  supersededByVerification, observeHttpPageTransition, URL, humanVerificationActive:false,scrollingCollectionStarted:false,
  cleanupOwnedInterception:async()=>{cleanups++;callsAtCleanup=calls;},
  httpBlock:null, process:{exitCode:0,exit:code=>{exitCode=code;throw manualExit;}},readLocalWarehouse:async()=>({}),confirmsLocalWarehouse:()=>false,
  Date:{now:()=>tick}, now:()=>new Date(tick).toISOString(),
  sleep:async ms=>{tick+=ms;}, rand:(min)=>min,
  QUERY:'towels', ENTRY_MODE:'empty_search', INITIAL_PRODUCTS_TIMEOUT_SECONDS:180, finalStatus:null,
  appendEvent:async (status,details)=>events.push({status,...details}),
  writeSnapshot:async (status,details)=>snapshots.push({status,...details}),
  bootstrapPageState:page, scan:page, ensurePageGuards:async()=>{},
  navigateConfirmed:async()=>{reloads++;},
  submitHomeSearch:async()=>({submitted:true}), bootstrapFromSafeTab:false,recoveredPreviousSearchTab:false,
  TEMU_EMPTY_SEARCH:'https://www.temu.com/search_result.html', TEMU_EMPTY_SEARCH_URL:/^https:\/\/(?:www\.)?temu\.com\/search_result\.html(?:[?#].*)?$/i,
  products:new Map(), IDLE_REQUIRED:10, MAX_PRODUCTS:50, consecutiveNoNewRounds:0,
  clicksSinceRecovery:0, bidiStatus:'ACTIVE', trafficSavingMode:false,
  bidi:{close:()=>{}}, console:{log:()=>{}}, OUT:'fixture', clicks:0,imageBlocked:0,
});
vm.runInContext(guard+loops+initial,context);
try {
  if (testCase.entry === 'waitForInitialProducts') {
    await vm.runInContext('(async()=>{'+driver+'})()',context);
  } else {
    const call = testCase.entry === 'bootstrapThroughEmptySearch'
      ? 'bootstrapThroughEmptySearch({navigate:false})' : testCase.entry+'()';
    await vm.runInContext('(async()=>{try{await '+call+';}catch(error){if(!(error instanceof InitialProductsStopped))throw error;}})()',context);
  }
} catch(error) {if(error!==manualExit)throw error;}
console.log(JSON.stringify({status:context.finalStatus,tick,calls,reloads,cleanups,callsAtCleanup,exitCode,snapshots,events}));
"""


@pytest.mark.parametrize("entry", [
    "waitForStableEmptySearch", "waitForPostVerificationSearchBox",
    "bootstrapThroughEmptySearch", "waitForInitialProducts",
])
@pytest.mark.parametrize("failure,expected", [
    ("network", "STOPPED_PROXY_OR_NETWORK_FAILURE"),
    ("denied", "STOPPED_ACCESS_DENIED"),
    ("timeout", "STOPPED_INITIAL_PRODUCTS_TIMEOUT"),
])
def test_every_initial_wait_path_stops_and_preserves_browser(entry, failure, expected):
    result = run_node(f"const testCase={json.dumps({'entry': entry, 'failure': failure})};" + HARNESS)
    assert result["status"] == expected
    terminal = result["snapshots"][-1]
    assert terminal["status"] == expected
    assert terminal["manual_action_required"] is (failure != "timeout")
    assert terminal["browser_preserved"] is True
    assert terminal["reason"] == expected.removeprefix("STOPPED_")
    assert terminal["stop_reason"] == terminal["reason"]
    assert result["tick"] <= 182000
    assert not any(item["status"] in {"RUNNING", "KEYWORD_COMPLETE", "KEYWORD_LIMIT_REACHED"}
                   for item in result["snapshots"])


def test_bootstrap_captcha_hands_off_without_spending_initial_wait_budget():
    result = run_node('const testCase={entry:"bootstrapThroughEmptySearch",failure:"timeout",manual:true};' + HARNESS)
    assert result["status"] == "WAITING_HUMAN_VERIFICATION"
    assert result["exitCode"] == 2 and result["cleanups"] == 1
    assert result["tick"] < 180000 and result["calls"] == result["callsAtCleanup"]
    assert result["snapshots"][-1]["collector_paused"] is True
    assert result["snapshots"][-1]["browser_preserved"] is True
    assert not any(item["status"] in {"RUNNING", "KEYWORD_COMPLETE", "KEYWORD_LIMIT_REACHED"}
                   for item in result["snapshots"])


@pytest.mark.parametrize('recovers', [True, False])
def test_disabled_empty_search_reloads_once_without_resetting_timeout(recovers):
    case={'entry':'waitForStableEmptySearch','disabled':True,'recoverAfterReload':recovers}
    result=run_node(f'const testCase={json.dumps(case)};' + HARNESS)
    assert result['reloads']==1
    assert result['status']==(None if recovers else 'STOPPED_INITIAL_PRODUCTS_TIMEOUT')
    assert result['tick'] <= (32000 if recovers else 182000)
    assert any(e['status']=='DISABLED_SEARCH_BOX_RELOAD' for e in result['events'])
