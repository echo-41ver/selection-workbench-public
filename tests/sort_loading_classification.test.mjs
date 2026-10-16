import test from 'node:test';
import assert from 'node:assert/strict';
import {classifyPageState} from '../scripts/temu_flower_recovery_policy.mjs';
test('loading search page with generic sign-in header is not a login wall',()=>{
 assert.equal(classifyPageState({url:'https://www.temu.com/search_result.html?search_key=remote',text:'Sign in / Register Sort by Top sales',visibleGoodsCount:0}),null);
});
test('visible login form and real login URL remain identifiable',()=>{
 assert.equal(classifyPageState({url:'https://www.temu.com/search_result.html',text:'Sign in',loginFormVisible:true,visibleGoodsCount:0}),'LOGIN_REQUIRED');
 assert.equal(classifyPageState({url:'https://www.temu.com/login.html',text:''}),'LOGIN_REQUIRED');
});
