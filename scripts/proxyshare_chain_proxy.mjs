import net from 'node:net';
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';

const listenHost = process.env.TEMU_CHAIN_LISTEN_HOST || '127.0.0.1';
const listenPort = Number(process.env.TEMU_CHAIN_LISTEN_PORT || 17891);
const preHost = process.env.TEMU_PREPROXY_HOST || '127.0.0.1';
const prePort = Number(process.env.TEMU_PREPROXY_PORT ?? 0);
const preProtocol = process.env.TEMU_PREPROXY_PROTOCOL || 'SOCKS5';
const proxyHost = process.env.PROXYSHARE_PROXY_HOST;
const proxyPort = Number(process.env.PROXYSHARE_PROXY_PORT || 5959);
const proxyUser = process.env.PROXYSHARE_PROXY_USER;
const proxyPassword = process.env.PROXYSHARE_PROXY_PASSWORD;
const activityDir = process.env.TEMU_CHAIN_ACTIVITY_DIR;
const worker = Number(process.env.TEMU_CHAIN_WORKER);
const configuredHandshakeMs = Number(process.env.TEMU_CHAIN_HANDSHAKE_TIMEOUT_MS ?? 12000);
const handshakeMs = Number.isSafeInteger(configuredHandshakeMs) && configuredHandshakeMs >= 1000 && configuredHandshakeMs <= 30000 ? configuredHandshakeMs : 12000;

function activityAllowed() {
  if (!activityDir || !Number.isInteger(worker) || worker < 1 || worker > 32) return false;
  try {
    const revokedFile = path.join(activityDir, 'revoked_at');
    const revoked = fs.existsSync(revokedFile) ? Number(fs.readFileSync(revokedFile, 'utf8')) : 0;
    if (!Number.isFinite(revoked)) return false;
    const now = Date.now() / 1000;
    return fs.readdirSync(activityDir).some(name => {
      if (!name.endsWith('.json')) return false;
      try {
        const lease = JSON.parse(fs.readFileSync(path.join(activityDir, name), 'utf8'));
        if (lease.worker !== worker || !['collector', 'manual_verification'].includes(lease.purpose)
          || !Number.isInteger(lease.pid) || lease.pid <= 0 || !Number.isFinite(lease.issued_at)
          || lease.issued_at <= revoked || !Number.isFinite(lease.expires_at)
          || lease.expires_at <= now || lease.expires_at > now + 15) return false;
        process.kill(lease.pid, 0);
        return true;
      } catch { return false; }
    });
  } catch { return false; }
}

function allowedTarget(value, tunnel = false) {
  try {
    const target = new URL(tunnel ? `https://${value}` : value);
    const hostname = target.hostname.toLowerCase().replace(/\.$/, '');
    const allowedHost = ['temu.com', 'kwcdn.com', 'temucdn.com'].some(domain => hostname === domain || hostname.endsWith(`.${domain}`));
    if (!allowedHost || target.username || target.password || target.hash) return null;
    if (tunnel && (target.protocol !== 'https:' || (target.port && target.port !== '443') || target.pathname !== '/' || target.search)) return null;
    if (!tunnel && (target.protocol !== 'http:' || (target.port && target.port !== '80'))) return null;
    target.hostname = hostname;
    return target;
  } catch { return null; }
}

function denial(source, status) {
  console.warn(JSON.stringify({event: 'CHAIN_DENIAL', worker, source, status, at: new Date().toISOString()}));
}

const connections = new Set();
function ownConnection(client, handshaking = false) {
  const connection = {client, upstream: null, closed: false, handshaking, close() {
    if (connection.closed) return;
    connection.closed = true;
    connections.delete(connection);
    connection.upstream?.destroy();
    client.destroy();
  }, discardUpstream() {
    const socket = connection.upstream;
    connection.upstream = null;
    socket?.destroy();
  }, attachUpstream(socket) {
    connection.upstream = socket;
    // Permanent listeners cover connect, handshake readers, unshift, piping,
    // and delayed ECONNRESET events after a reader removes its own listeners.
    const closeEstablished = () => {
      if (connection.upstream === socket && !connection.handshaking) connection.close();
    };
    socket.on('error', closeEstablished);
    socket.once('close', closeEstablished);
    if (connection.closed) socket.destroy();
    return socket;
  }};
  connections.add(connection);
  client.once('close', () => connection.close());
  client.once('end', () => { if (connection.handshaking) connection.close(); });
  client.on('error', () => connection.close());
  return connection;
}

function connected(socket) {
  return new Promise((resolve, reject) => {
    const cleanup = () => { socket.off('connect', ready); socket.off('error', failed); socket.off('close', closed); };
    const ready = () => { cleanup(); resolve(); };
    const failed = error => { cleanup(); reject(error); };
    const closed = () => failed(Object.assign(new Error('Upstream closed during connect'), {code: 'UPSTREAM_EOF'}));
    socket.once('connect', ready); socket.once('error', failed); socket.once('close', closed);
    if (socket.destroyed) closed();
  });
}
setInterval(() => {
  if (connections.size && !activityAllowed()) for (const connection of [...connections]) connection.close();
}, 200).unref();

if (!proxyHost || !proxyUser || !proxyPassword) {
  console.error('Missing ProxyShare environment variables.');
  process.exit(2);
}

function readExactly(socket, size) {
  return new Promise((resolve, reject) => {
    let buffer = Buffer.alloc(0);
    const onData = chunk => {
      buffer = Buffer.concat([buffer, chunk]);
      if (buffer.length >= size) {
        cleanup();
        if (buffer.length > size) socket.unshift(buffer.subarray(size));
        resolve(buffer.subarray(0, size));
      }
    };
    const onError = error => { cleanup(); reject(error); };
    const onEnd = () => { cleanup(); reject(Object.assign(new Error('SOCKS connection ended early'), {code: 'UPSTREAM_EOF'})); };
    const cleanup = () => {
      socket.pause();
      socket.off('data', onData);
      socket.off('error', onError);
      socket.off('end', onEnd);
      socket.off('close', onEnd);
    };
    socket.on('data', onData);
    socket.on('error', onError);
    socket.on('end', onEnd);
    socket.on('close', onEnd);
    if (socket.destroyed || socket.readableEnded) { onEnd(); return; }
    socket.resume();
  });
}

async function connectViaSocks(destinationHost, destinationPort, connection) {
  if (prePort === 0) {
    const direct = net.connect({host: destinationHost, port: destinationPort});
    connection.attachUpstream(direct);
    direct.setTimeout(30000, () => connection.close());
    await connected(direct);
    return direct;
  }
  const socket = net.connect({ host: preHost, port: prePort });
  connection.attachUpstream(socket);
  socket.setTimeout(30000, () => connection.close());
  await connected(socket);
  if (preProtocol === 'HTTP') {
    const authority = `${destinationHost}:${destinationPort}`;
    socket.write(`CONNECT ${authority} HTTP/1.1\r\nHost: ${authority}\r\nConnection: keep-alive\r\n\r\n`);
    const header = await readHeader(socket);
    const match = header.match(/^HTTP\/1\.[01]\s+(\d{3})(?:\s|$)/);
    if (!match || match[1] !== '200') {
      const error = new Error('Local HTTP proxy tunnel refused');
      error.proxyResponseReceived = Boolean(match);
      throw error; // Refusal must not trigger a different route or login/session.
    }
    return socket;
  }
  if (preProtocol !== 'SOCKS5') throw new Error('Unsupported local proxy protocol');
  socket.write(Buffer.from([0x05, 0x01, 0x00]));
  const greeting = await readExactly(socket, 2);
  if (greeting[0] !== 0x05 || greeting[1] !== 0x00) throw new Error('SOCKS5 negotiation failed');
  const hostBytes = Buffer.from(destinationHost, 'utf8');
  const request = Buffer.alloc(7 + hostBytes.length);
  request.set([0x05, 0x01, 0x00, 0x03, hostBytes.length], 0);
  hostBytes.copy(request, 5);
  request.writeUInt16BE(destinationPort, 5 + hostBytes.length);
  socket.write(request);
  const head = await readExactly(socket, 4);
  if (head[1] !== 0x00) throw new Error(`SOCKS5 connect failed: ${head[1]}`);
  const atyp = head[3];
  if (atyp === 0x01) await readExactly(socket, 6);
  else if (atyp === 0x04) await readExactly(socket, 18);
  else if (atyp === 0x03) {
    const len = await readExactly(socket, 1);
    await readExactly(socket, len[0] + 2);
  } else throw new Error('Unexpected SOCKS5 address type');
  return socket;
}

function proxyAuthorization() {
  return `Basic ${Buffer.from(`${proxyUser}:${proxyPassword}`).toString('base64')}`;
}

function readHeader(socket) {
  return new Promise((resolve, reject) => {
    let data = Buffer.alloc(0);
    const onData = chunk => {
      data = Buffer.concat([data, chunk]);
      const end = data.indexOf('\r\n\r\n');
      if (end !== -1) {
        cleanup();
        const rest = data.subarray(end + 4);
        if (rest.length) socket.unshift(rest);
        resolve(data.subarray(0, end + 4).toString('latin1'));
      } else if (data.length > 65536) {
        cleanup();
        reject(new Error('Proxy response header too large'));
      }
    };
    const onError = error => {
      // A partial HTTP denial is still a response, never a network retry.
      error.proxyResponseReceived = /^HTTP\/1\.[01]\s+\d{3}(?:\s|$)/.test(data.toString('latin1'));
      cleanup(); reject(error);
    };
    const onEnd = () => onError(Object.assign(new Error('Proxy connection ended before headers'), {code: 'UPSTREAM_EOF'}));
    const cleanup = () => { socket.pause(); socket.off('data', onData); socket.off('error', onError); socket.off('close', onEnd); socket.off('end', onEnd); };
    socket.on('data', onData);
    socket.on('error', onError);
    socket.on('close', onEnd);
    socket.on('end', onEnd);
    if (socket.destroyed || socket.readableEnded) { onEnd(); return; }
    socket.resume();
  });
}

async function openTunnel(authority, connection) {
  const deadline = Date.now() + 3 * handshakeMs;
  for (let attempt = 0; attempt < 3; attempt++) {
    if (connection.closed || !activityAllowed()) { connection.close(); return null; }
    const timeout = setTimeout(() => {
      connection.upstream?.destroy(Object.assign(new Error('Proxy handshake timed out'), {code: 'ETIMEDOUT'}));
    }, Math.min(handshakeMs, Math.max(1, deadline - Date.now())));
    try {
      const upstream = await connectViaSocks(proxyHost, proxyPort, connection);
      if (connection.closed || !activityAllowed()) { connection.close(); return null; }
      upstream.write(
        `CONNECT ${authority} HTTP/1.1\r\nHost: ${authority}\r\n` +
        `Proxy-Authorization: ${proxyAuthorization()}\r\nProxy-Connection: Keep-Alive\r\n\r\n`,
      );
      const responseHeader = await readHeader(upstream);
      return {upstream, responseHeader};
    } catch (error) {
      connection.discardUpstream();
      if (connection.closed || !activityAllowed()) { connection.close(); return null; }
      const retryable = ['ECONNRESET', 'UPSTREAM_EOF', 'ETIMEDOUT'].includes(error.code)
        && !error.proxyResponseReceived;
      if (!retryable || attempt === 2 || Date.now() >= deadline) throw error;
    } finally {
      clearTimeout(timeout);
    }
  }
}

const server = http.createServer(async (req, res) => {
  const target = allowedTarget(req.url);
  if (!target || !activityAllowed()) {
    denial(target ? 'LOCAL_ACTIVITY_REQUIRED' : 'LOCAL_TARGET_NOT_ALLOWED', 403);
    res.writeHead(403, {'content-type': 'text/plain', connection: 'close'});
    res.end('TEMU collection activity required');
    return;
  }
  const connection = ownConnection(req.socket);
  try {
    const upstream = await connectViaSocks(proxyHost, proxyPort, connection);
    if (req.socket.destroyed || !activityAllowed()) { connection.close(); return; }
    const headers = { ...req.headers, host: target.host, 'proxy-authorization': proxyAuthorization(), connection: 'close' };
    const lines = [`${req.method} ${target.href} HTTP/${req.httpVersion}`];
    for (const [name, value] of Object.entries(headers)) lines.push(`${name}: ${value}`);
    upstream.write(`${lines.join('\r\n')}\r\n\r\n`);
    const closeBoth = () => {
      if (!upstream.destroyed) upstream.destroy();
      if (res.socket && !res.socket.destroyed) res.socket.destroy();
    };
    upstream.on('error', closeBoth);
    res.socket?.on('error', closeBoth);
    req.pipe(upstream);
    upstream.pipe(res.socket);
  } catch (error) {
    console.error(`HTTP chain error: ${error.message}`);
    if (!res.destroyed && !res.headersSent) {
      res.writeHead(502, { 'content-type': 'text/plain' });
      res.end('Upstream proxy connection failed', () => connection.close());
    } else connection.close();
    connection.upstream?.destroy();
  }
});

// Rejected CONNECT requests never reach ownConnection(). Their socket can
// still emit ECONNRESET after end(), so install the listener at accept time.
server.on('connection', socket => {
  socket.on('error', () => socket.destroy());
});

server.on('connect', async (req, clientSocket, head) => {
  const target = allowedTarget(req.url, true);
  if (!target || !activityAllowed()) {
    denial(target ? 'LOCAL_ACTIVITY_REQUIRED' : 'LOCAL_TARGET_NOT_ALLOWED', 403);
    clientSocket.end('HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: 0\r\n\r\n');
    return;
  }
  const connection = ownConnection(clientSocket, true);
  try {
    const authority = `${target.hostname}:443`;
    const tunnel = await openTunnel(authority, connection);
    if (!tunnel) return;
    const {upstream, responseHeader} = tunnel;
    const status = Number(responseHeader.split(' ')[1]);
    if (status !== 200) {
      denial('UPSTREAM_PROXY_CONNECT_RESPONSE', Number.isInteger(status) && status >= 100 && status <= 599 ? status : null);
      clientSocket.end(responseHeader, () => connection.close());
      connection.discardUpstream();
      return;
    }
    if (clientSocket.destroyed || upstream.destroyed || !activityAllowed()) { connection.close(); return; }
    connection.handshaking = false;
    upstream.setTimeout(0);
    clientSocket.write('HTTP/1.1 200 Connection Established\r\n\r\n');
    if (head.length) upstream.write(head);
    const closeBoth = () => {
      if (!upstream.destroyed) upstream.destroy();
      if (!clientSocket.destroyed) clientSocket.destroy();
    };
    upstream.on('error', closeBoth);
    clientSocket.on('error', closeBoth);
    upstream.pipe(clientSocket);
    clientSocket.pipe(upstream);
  } catch (error) {
    console.error(`CONNECT chain error: ${error.message}`);
    if (!clientSocket.destroyed) clientSocket.end('HTTP/1.1 502 Bad Gateway\r\n\r\n', () => connection.close());
    else connection.close();
    connection.upstream?.destroy();
  }
});

server.on('clientError', (_error, socket) => {
  if (!socket.destroyed) socket.destroy();
});

server.listen(listenPort, listenHost, () => {
  console.log(`TEMU ProxyShare chain listening on ${listenHost}:${listenPort}`);
});
