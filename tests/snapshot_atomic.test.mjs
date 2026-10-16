import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {atomicSnapshot} from '../scripts/snapshot_atomic.mjs';

test('temporary reader locks retry without exposing partial or missing snapshots',async()=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'snapshot-lock-'));
  try {
    const file=path.join(dir,'products.jsonl');await fs.writeFile(file,'old\n');let calls=0;
    await atomicSnapshot(file,'new\n',{sleep:async()=>{},filesystem:{...fs,rename:async(...args)=>{
      assert.equal(await fs.readFile(file,'utf8'),'old\n');
      if(++calls<4)throw Object.assign(new Error('reader holds file'),{code:['EPERM','EACCES','EBUSY'][calls-1]});
      return fs.rename(...args);
    }}});
    assert.equal(calls,4);assert.equal(await fs.readFile(file,'utf8'),'new\n');
  }finally{await fs.rm(dir,{recursive:true,force:true});}
});

test('permanent errors and exhausted lock retries preserve the last complete snapshot',async()=>{
  for(const [code,expected] of [['ENOENT',1],['EPERM',3]]){
    const dir=await fs.mkdtemp(path.join(os.tmpdir(),'snapshot-error-'));
    try {
      const file=path.join(dir,'products.jsonl');await fs.writeFile(file,'old\n');let calls=0;
      await assert.rejects(()=>atomicSnapshot(file,'new\n',{attempts:3,sleep:async()=>{},
        filesystem:{...fs,rename:async()=>{calls++;throw Object.assign(new Error('failure'),{code});}}}),{code});
      assert.equal(calls,expected);assert.equal(await fs.readFile(file,'utf8'),'old\n');
    }finally{await fs.rm(dir,{recursive:true,force:true});}
  }
});
