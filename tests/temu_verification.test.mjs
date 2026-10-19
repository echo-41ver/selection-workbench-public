import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {inspectHumanVerification} from '../scripts/temu_verification.mjs';
import {classifyPageState} from '../scripts/temu_flower_recovery_policy.mjs';
import {isActiveVerificationState} from '../scripts/temu_search_bootstrap_policy.mjs';

test('verification words in a product listing do not block normal collection',()=>{
 const state={url:'https://www.temu.com/search_result.html?search_key=washer',
  text:'Security verification tool, 40 products',visibleGoodsCount:40,verificationVisible:false};
 assert.equal(classifyPageState(state),null);assert.equal(isActiveVerificationState(state),false);
 assert.equal(classifyPageState({...state,verificationVisible:true}),'HUMAN_VERIFICATION');
 assert.equal(classifyPageState({...state,url:'https://www.temu.com/bgn_verification.html'}),'HUMAN_VERIFICATION');
});

test('hidden challenge elements do not block, visible challenge controls do',()=>{
 let visible=false;
 const challenge={innerText:'Verify you are human',getBoundingClientRect:()=>({width:visible?200:0,height:100}),querySelector:()=>({})};
 const context=vm.createContext({getComputedStyle:()=>({display:'block',visibility:'visible',opacity:'1'}),
  document:{querySelectorAll:selector=>selector.startsWith('iframe')?[]:[challenge]}});
 const probe=()=>vm.runInContext(`(${inspectHumanVerification.toString()})()`,context);
 assert.equal(probe(),false);visible=true;assert.equal(probe(),true);
 challenge.innerText='Product security verification accessory';challenge.querySelector=()=>null;
 assert.equal(probe(),false);
});
