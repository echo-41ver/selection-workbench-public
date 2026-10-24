import {createCategoryRelevance} from './category_relevance.mjs';
process.stdin.setEncoding('utf8');
let input='';for await(const chunk of process.stdin)input+=chunk;
const {category,rows}=JSON.parse(input);const policy=createCategoryRelevance(category);
const result={accepted:[],excluded:[],unclassified:[],version:policy.version,fingerprint:policy.fingerprint};
for(const row of rows){const decision=policy.classify(row);
 const enriched={...row,category_relevance:{...decision,policy_version:policy.version,policy_fingerprint:policy.fingerprint}};
 result[decision.state==='RELEVANT'?'accepted':decision.state==='EXCLUDED'?'excluded':'unclassified'].push(enriched);
}
process.stdout.write(JSON.stringify(result));
