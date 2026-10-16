import test from 'node:test';
import assert from 'node:assert/strict';
import {createCategoryRelevance, COLLECTION_PROGRESS_VERSION, restoreNoNewRounds} from '../scripts/category_relevance.mjs';

const mouseConfig = {category_name_zh:'鼠标',temu:{include_terms:['mouse','mice'],
  severe_exclude_terms:['mouse pad','mousepad','keyboard']},stage1:{attributes:[{
    name:'product_form',hard_conflict:true,values:{mouse:['mouse','mice','滑鼠'],pad:['mouse pad','鼠标垫']},
  },{name:'connectivity',hard_conflict:true,values:{wireless:['wireless','无线']}}]}};
const mouse = createCategoryRelevance(mouseConfig);

test('old or changed relevance rules cannot restore an exhausted empty-round counter', () => {
  const saved={stop_logic_version:COLLECTION_PROGRESS_VERSION,relevance_policy_fingerprint:mouse.fingerprint,consecutive_no_new_rounds:10};
  assert.equal(restoreNoNewRounds(saved,mouse),10);
  assert.equal(restoreNoNewRounds({...saved,stop_logic_version:'relevant-round-v3'},mouse),0);
  const changed=createCategoryRelevance({...mouseConfig,category_aliases:['電腦滑鼠']});
  assert.equal(restoreNoNewRounds(saved,changed),0);
  assert.equal(restoreNoNewRounds({...saved,consecutive_no_new_rounds:-1},mouse),0);
  assert.equal(restoreNoNewRounds({...saved,consecutive_no_new_rounds:Infinity},mouse),0);
});

test('uses the configured Chinese category without a translation API', () => {
  assert.equal(mouse.classify({title:'商品图片 2.4G 静音可充电无线鼠标'}).state,'RELEVANT');
  assert.equal(mouse.classify({title:'商品图片 银色雕刻有线滑鼠'}).state,'RELEVANT');
});
test('expands only the uniquely anchored approved identity group', () => {
  assert.ok(mouse.includeTerms.includes('滑鼠'));
  assert.ok(!mouse.includeTerms.includes('无线'));
  assert.ok(!mouse.includeTerms.includes('鼠标垫'));
  assert.equal(mouse.classify({title:'无线手机耳机'}).state,'UNKNOWN');
});
test('respects complete Latin words and normalized casing and width', () => {
  assert.equal(mouse.classify({title:'ＭＯＵＳＥ wireless gaming'}).state,'RELEVANT');
  assert.equal(mouse.classify({title:'Mousepad large desktop surface'}).state,'EXCLUDED');
  assert.equal(mouse.classify({title:'Mousey printed toy'}).state,'UNKNOWN');
});
test('a more specific exclusion cannot be overridden by its generic noun', () => {
  assert.equal(mouse.classify({title:'Gaming mouse pad with RGB lights'}).state,'EXCLUDED');
  assert.equal(mouse.classify({title:'Mechanical keyboard'}).state,'EXCLUDED');
});
test('unconfigured accessory compounds are unknown and kept, not main products', () => {
  for (const title of ['大号鼠标垫','鼠标保护壳','无线鼠标收纳盒','USB mouse cable']) {
    const result = mouse.classify({title});
    assert.equal(result.state,'UNKNOWN',title);
    assert.equal(result.reason,'ACCESSORY_OR_COMPATIBILITY_CONTEXT',title);
    assert.equal(mouse.isSevereOffTopic({title}),false,title);
  }
});
test('compatibility or negation mentions do not establish the sold main item', () => {
  for (const title of ['Replacement case for wireless mouse','收纳包，适用于无线鼠标','不含鼠标'])
    assert.equal(mouse.classify({title}).state,'UNKNOWN',title);
});
test('a main item plus an explicit excluded item stays ambiguous', () => {
  assert.equal(mouse.classify({title:'Wireless mouse and keyboard combo'}).state,'UNKNOWN');
  assert.equal(mouse.classify({title:'Gaming mouse with a mouse pad'}).state,'UNKNOWN');
});
test('supports accessory categories themselves with their full approved nouns', () => {
  const cases = createCategoryRelevance({category_name_zh:'手机壳',temu:{include_terms:['phone case']}});
  assert.equal(cases.classify({title:'透明防摔手机壳'}).state,'RELEVANT');
  assert.equal(cases.classify({title:'Shockproof phone case'}).state,'RELEVANT');
});
test('supports unrelated categories and explicit multilingual aliases without mouse rules', () => {
  const drill = createCategoryRelevance({category_name_zh:'电钻',category_aliases:['電鑽'],
    temu:{include_terms:['power drill'],category_aliases:['taladro'],severe_exclude_terms:['nail drill']}});
  for (const title of ['充电电钻','20V 電鑽','Taladro inalámbrico']) assert.ok(drill.isRelated({title}),title);
  assert.equal(drill.classify({title:'游戏鼠标'}).state,'UNKNOWN');
});
test('empty or unsupported vocabulary never marks all items relevant or stalled', () => {
  const empty = createCategoryRelevance({temu:{base_queries:['wireless mouse']}});
  assert.equal(empty.classify({title:'无线鼠标'}).state,'UNKNOWN');
  assert.deepEqual(empty.summarize([{title:'无线鼠标'},{title:'未知产品'}]),
    {relevant:0,unknown:2,excluded:0,progress:2});
  const english = createCategoryRelevance({temu:{include_terms:['kettle']}});
  assert.equal(english.countProgressRows([{title:'电热水壶'}]),1);
});
test('unknown-language items count as collection progress but not relevant items', () => {
  assert.deepEqual(mouse.summarize([{title:'无线鼠标'},{title:'ラップトップ用マウス'},{title:'Keyboard'}]),
    {relevant:1,unknown:1,excluded:1,progress:2});
  assert.equal(mouse.countProgressRows([]),0);
});
test('uses card text when title is absent, and prefers actual title over card noise', () => {
  assert.equal(mouse.classify({card_text:'无线鼠标'}).state,'RELEVANT');
  assert.equal(mouse.classify({title:'键盘',card_text:'推荐鼠标'}).state,'UNKNOWN');
});
test('rule fingerprint changes with semantics but not input ordering', () => {
  const first = createCategoryRelevance({category_name_zh:'鼠标',temu:{include_terms:['mouse','mice']}});
  const reordered = createCategoryRelevance({category_name_zh:'鼠标',temu:{include_terms:['mice','mouse']}});
  const changed = createCategoryRelevance({category_name_zh:'鼠标',temu:{include_terms:['mice','mouse'],category_aliases:['滑鼠']}});
  assert.equal(first.fingerprint,reordered.fingerprint);
  assert.notEqual(first.fingerprint,changed.fingerprint);
  assert.match(first.fingerprint,/^[a-f0-9]{64}$/);
});
test('reviewed regional category names are data, not an implicit translation table', () => {
  const plain=createCategoryRelevance({category_name_zh:'鼠标',temu:{include_terms:['mouse']}});
  assert.equal(plain.classify({title:'有线滑鼠'}).state,'UNKNOWN');
  const reviewed=createCategoryRelevance({category_name_zh:'鼠标',temu:{include_terms:['mouse'],
    category_aliases:{'zh-CN':['鼠标'],'zh-TW':['鼠標','滑鼠'],en:['mouse','mice'],description:['键盘']}}});
  for (const title of ['有线滑鼠','無線鼠標','MOUSE']) assert.equal(reviewed.classify({title}).state,'RELEVANT',title);
  assert.equal(reviewed.classify({title:'机械键盘'}).state,'UNKNOWN');
  assert.equal(reviewed.classify({title:'飛鼠遙控器'}).state,'UNKNOWN');
  assert.notEqual(reviewed.fingerprint,plain.fingerprint);
});
test('Unicode presentation differences normalize without inventing translations', () => {
  assert.equal(mouse.classify({title:'無線鼠\u200b标'}).state,'RELEVANT');
  assert.equal(mouse.classify({title:'ＭＯＵＳＥ'}).state,'RELEVANT');
  const curtain=createCategoryRelevance({temu:{include_terms:['shower curtain']}});
  assert.equal(curtain.classify({title:'shower-curtain'}).state,'RELEVANT');
});
test('regional aliases retain accessory and suitability ambiguity', () => {
  const reviewed=createCategoryRelevance({category_name_zh:'鼠标',temu:{category_aliases:['鼠標','滑鼠']}});
  for (const title of ['滑鼠墊','鼠標保護殼','滑鼠收納包','適用於滑鼠','滑鼠替換件'])
    assert.equal(reviewed.classify({title}).state,'UNKNOWN',title);
});
test('includes and packaging words are not a bag accessory suffix', () => {
  assert.equal(mouse.classify({title:'无线鼠标 - 包含迷你USB接收器'}).state,'RELEVANT');
  assert.equal(mouse.classify({title:'鼠标包装含USB接收器'}).state,'RELEVANT');
  assert.equal(mouse.classify({title:'无线鼠标包'}).state,'UNKNOWN');
});
test('a new title clause does not inherit the previous compatibility qualifier', () => {
  assert.equal(mouse.classify({title:'MX 4 for Mac - 无线蓝牙鼠标'}).state,'RELEVANT');
  assert.equal(mouse.classify({title:'Replacement case for wireless mouse'}).state,'UNKNOWN');
});

const subjectMouse=createCategoryRelevance({category_name_zh:'鼠标',temu:{include_terms:['mouse'],category_aliases:['鼠標','滑鼠'],
  subject_filter:{mode:'TARGET_BODY_ONLY',reject_mixed_subjects:true,
    exclude_subject_terms:['键盘','鍵盤','keyboard','键鼠','平板电脑','空中鼠标','飞鼠','touchpad','鼠标垫']}}});
test('clear other sold bodies and mixed bundles are excluded even when mouse is mentioned',()=>{
 for(const title of ['机械键盘','無線鍵盤','Wireless keyboard and mouse combo','蓝牙键盘和滑鼠套装',
  '安卓平板电脑12GB，附键盘和鼠标','二合一无线空中鼠标遥控器','无线飞鼠','触摸板touchpad for computer mouse'])
  assert.equal(subjectMouse.classify({title}).state,'EXCLUDED',title);
});
test('compatibility hosts and keyboard shortcuts do not exclude a real mouse',()=>{
 for(const title of ['无线鼠标，适用于笔记本电脑、平板电脑和键盘','Wireless mouse compatible with keyboard',
  '鼠标支持键盘快捷键宏','适用于笔记本电脑的无线鼠标','无线鼠标包含USB接收器'])
  assert.equal(subjectMouse.classify({title}).state,'RELEVANT',title);
});
test('clear target accessories are excluded while incomplete subject text remains unknown',()=>{
 for(const title of ['鼠标垫','滑鼠收納包','Mouse receiver','鼠标保护壳'])
  assert.equal(subjectMouse.classify({title}).state,'EXCLUDED',title);
 for(const title of ['商品图片','攻击鲨X3MAX','2 - 太空灰'])assert.equal(subjectMouse.classify({title}).state,'UNKNOWN',title);
 assert.equal(subjectMouse.classify({title:'商品图片'}).reason,'MISSING_PRODUCT_TEXT');
});
test('subject filter changes invalidate old saturation counts',()=>{
 assert.notEqual(subjectMouse.fingerprint,mouse.fingerprint);
 assert.equal(restoreNoNewRounds({stop_logic_version:COLLECTION_PROGRESS_VERSION,relevance_policy_fingerprint:mouse.fingerprint,consecutive_no_new_rounds:10},subjectMouse),0);
});
