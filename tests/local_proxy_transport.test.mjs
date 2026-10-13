import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

// Execute the actual chain transport function with fake sockets. No listener,
// upstream gateway, browser, or external website is contacted by these tests.
const source=fs.readFileSync(new URL('../scripts/proxyshare_chain_proxy.mjs',import.meta.url),'utf8');
const body=source.slice(source.indexOf('async function connectViaSocks('),source.indexOf('function proxyAuthorization('));
function fixture(protocol,header='HTTP/1.1 200 Connection Established\r\n\r\n',port=45678){
  const messages=[],addresses=[];
  const socket={setTimeout(){},write(value){messages.push(value);}};
  let reads=0;
  const fn=vm.runInNewContext(body+'\nconnectViaSocks',{
    Buffer,Error,preHost:'::1',prePort:port,preProtocol:protocol,
    net:{connect(address){addresses.push(address);return socket;}},
    connected:async()=>{},readHeader:async()=>header,
    readExactly:async(s,n)=>++reads===1?Buffer.from([5,0]):reads===2?Buffer.from([5,0,0,1]):Buffer.alloc(n),
  });
  const connection={attachUpstream(){},close(){}};
  return {fn,socket,connection,messages,addresses};
}
test('HTTP-only proxy connects only to selected local endpoint and tunnels gateway',async()=>{
  const f=fixture('HTTP');
  assert.equal(await f.fn('gateway.test',5959,f.connection),f.socket);
  assert.deepEqual(JSON.parse(JSON.stringify(f.addresses)),[{host:'::1',port:45678}]);
  assert.equal(f.messages[0],'CONNECT gateway.test:5959 HTTP/1.1\r\nHost: gateway.test:5959\r\nConnection: keep-alive\r\n\r\n');
  assert.equal(f.messages.length,1);
  assert.ok(!f.messages[0].includes('Authorization'));
});
for(const status of ['403','407','502']){
  test(`local HTTP ${status} refusal is terminal for transport and cannot change route`,async()=>{
    const f=fixture('HTTP',`HTTP/1.1 ${status} Refused\r\n\r\n`);
    await assert.rejects(f.fn('gateway.test',5959,f.connection),error=>error.proxyResponseReceived===true);
    assert.equal(f.addresses.length,1);
    assert.equal(f.messages.length,1);
  });
}
test('invalid HTTP reply fails without inventing acceptance',async()=>{
  const f=fixture('HTTP','not a proxy response');
  await assert.rejects(f.fn('gateway.test',5959,f.connection),error=>error.proxyResponseReceived===false);
  assert.equal(f.addresses.length,1);
});
test('SOCKS5 route uses selected nondefault port and literal gateway target',async()=>{
  const f=fixture('SOCKS5');
  await f.fn('gateway.test',5959,f.connection);
  assert.deepEqual([...f.messages[0]],[5,1,0]);
  assert.ok(f.messages[1].includes(Buffer.from('gateway.test')));
  assert.equal(f.addresses[0].port,45678);
});
test('explicit direct/system route does not use local proxy handshake',async()=>{
  const f=fixture('SYSTEM_ROUTE',undefined,0);
  await f.fn('gateway.test',5959,f.connection);
  assert.deepEqual(JSON.parse(JSON.stringify(f.addresses)),[{host:'gateway.test',port:5959}]);
  assert.equal(f.messages.length,0);
});
test('unknown local proxy protocol is rejected without silent SOCKS fallback',async()=>{
  const f=fixture('UNKNOWN');
  await assert.rejects(f.fn('gateway.test',5959,f.connection));
  assert.equal(f.messages.length,0);
});
