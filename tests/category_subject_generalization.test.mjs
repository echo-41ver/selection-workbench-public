import test from 'node:test';import assert from 'node:assert/strict';
import {createCategoryRelevance} from '../scripts/category_relevance.mjs';
const cases=[
 {name:'鼠标',aliases:['鼠标','鼠標','滑鼠','computer mouse'],exclude:['键盘','keyboard','平板电脑'],
  good:['無線滑鼠','Wireless computer mouse compatible with keyboard'],bad:['机械键盘','键盘鼠标组合','平板电脑带鼠标']},
 {name:'锅铲',aliases:['锅铲','鍋鏟','硅胶铲','spatula'],exclude:['汤勺','湯勺','soup ladle','平底锅','frying pan'],
  good:['耐高温硅胶铲','Silicone spatula for frying pan'],bad:['不锈钢汤勺','Frying pan with spatula']},
 {name:'电钻',aliases:['电钻','電鑽','power drill'],exclude:['电锯','電鋸','power saw','钻头','鑽頭','drill bit','磨甲机','nail drill'],
  good:['充电電鑽','Cordless power drill'],bad:['电锯','Power drill bit replacement','Nail drill manicure tool']},
 {name:'手机壳',aliases:['手机壳','手機殼','phone case'],exclude:['手机','手機','phone','充电线','charging cable'],
  good:['透明手機殼','Shockproof phone case for phone'],bad:['智能手机','Phone with free phone case','Charging cable']},
];
for(const scenario of cases)test(`same classifier handles ${scenario.name} and preserves the sold subject`,()=>{
 const policy=createCategoryRelevance({category_name_zh:scenario.name,temu:{category_aliases:scenario.aliases,
  subject_filter:{mode:'TARGET_BODY_ONLY',reject_mixed_subjects:true,exclude_subject_terms:scenario.exclude}}});
 for(const title of scenario.good)assert.equal(policy.classify({title}).state,'RELEVANT',title);
 for(const title of scenario.bad)assert.equal(policy.classify({title}).state,'EXCLUDED',title);
 assert.equal(policy.classify({title:'型号X3MAX'}).state,'UNKNOWN');
});
