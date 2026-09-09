'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs');
const http = require('node:http');
const { execFile } = require('node:child_process');
const { createHandlers } = require('../webui/routes.js');
const { isLoopbackHostname } = require('../security/loopback.cjs');
const { createTicketLifecycleProcess } = require('../ticket_lifecycle/adapter.cjs');

const DEFAULT_PORT = 43121;
const DEFAULT_FLEET_URL = 'http://127.0.0.1:8899';
const MAX_BODY_BYTES = 64 * 1024;
const MAX_FLEET_BODY_BYTES = 512 * 1024;
const TOKEN_HEADER = 'x-pursers-home-token';
const SAFE_BOARD = /^[A-Za-z0-9._-]{1,80}$/;
const RUNTIME_ENV = [
  'AIONUI_BASE_URL',
  'AIONUI_USER_ID',
  'AIONUI_CONVERSATION_ID',
  'AIONUI_RUNTIME_TOKEN',
];

function fail(message) {
  throw new Error(message);
}

function parseInteger(value, label, minimum, maximum) {
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < minimum || parsed > maximum) {
    fail(`${label} must be an integer from ${minimum} to ${maximum}`);
  }
  return parsed;
}

function normalizeLoopbackOrigin(value, label) {
  let url;
  try {
    url = new URL(value);
  } catch (_error) {
    fail(`${label} must be an absolute loopback HTTP origin`);
  }
  if (url.protocol !== 'http:' || !isLoopbackHostname(url.hostname)) {
    fail(`${label} must use HTTP on a loopback hostname`);
  }
  if (url.username || url.password || url.pathname !== '/' || url.search || url.hash) {
    fail(`${label} must not include credentials, a path, a query, or a fragment`);
  }
  return url.origin;
}

function normalizeOrigin(value) {
  return normalizeLoopbackOrigin(value, '--origin');
}

function readTokenFile(tokenFile) {
  const info = fs.lstatSync(tokenFile);
  if (!info.isFile() || info.isSymbolicLink()) fail('--token-file must be a regular file, not a symlink');
  if (process.platform !== 'win32' && (info.mode & 0o077) !== 0) {
    fail('--token-file must not be readable or writable by group or other users');
  }
  if (info.size < 32 || info.size > 1024) fail('--token-file must contain 32-1024 bytes');
  const token = fs.readFileSync(tokenFile, 'utf8').trim();
  if (token.length < 32 || token.length > 512 || /\s/.test(token)) {
    fail('--token-file must contain one 32-512 character token without whitespace');
  }
  return token;
}

function safeEqual(left, right) {
  const leftBytes = Buffer.from(String(left || ''));
  const rightBytes = Buffer.from(String(right || ''));
  return leftBytes.length === rightBytes.length && crypto.timingSafeEqual(leftBytes, rightBytes);
}

function bridgeArguments(args, stateDir) {
  if (!stateDir) return args;
  if (args[0] === 'status') return ['status', '--state-dir', stateDir, ...args.slice(1)];
  if (args[0] === 'join' && args.length >= 2) {
    return ['join', ...args.slice(1, -1), '--state-dir', stateDir, args.at(-1)];
  }
  return args;
}

function runCommand(command, args, options = {}) {
  return new Promise((resolve, reject) => {
    execFile(command, args, {
      encoding: 'utf8',
      maxBuffer: 4 * 1024 * 1024,
      timeout: 30000,
      ...options,
    }, (error, stdout, stderr) => {
      if (error) {
        error.stderr = String(stderr || '').slice(0, 4096);
        reject(error);
        return;
      }
      resolve(stdout);
    });
  });
}

function createBridgeRunner(command, stateDir) {
  return (args, options = {}) => runCommand(
    command,
    bridgeArguments(args, stateDir),
    { env: { ...process.env, ...(options.env || {}) } },
  );
}

function createTeamRunner(command) {
  return (args, input) => new Promise((resolve) => {
    const env = { ...process.env };
    for (const name of RUNTIME_ENV) delete env[name];
    const child = execFile(command, ['team', ...args], {
      encoding: 'utf8',
      maxBuffer: 4 * 1024 * 1024,
      timeout: 30000,
      env,
    }, (error, stdout) => {
      const text = String(stdout || '').trim();
      if (text) {
        try {
          resolve(JSON.parse(text));
          return;
        } catch (_parseError) {
          resolve({ success: false, error: { code: 'transport_unavailable', message: 'AionCore returned invalid JSON.' } });
          return;
        }
      }
      resolve({
        success: false,
        error: { code: 'transport_unavailable', message: error ? error.message : 'AionCore returned no result.' },
      });
    });
    child.stdin.on('error', () => {});
    child.stdin.end(JSON.stringify(input || {}));
  });
}

function createFleetResultsFetcher(baseUrl, fetchImpl = globalThis.fetch) {
  const origin = normalizeLoopbackOrigin(baseUrl, '--fleet-url');
  if (typeof fetchImpl !== 'function') fail('Fleet result fetch is unavailable');
  return async (board) => {
    if (!SAFE_BOARD.test(board || '')) fail('result board must be a safe identifier');
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 5000);
    try {
      const response = await fetchImpl(
        `${origin}/api/board/${encodeURIComponent(board)}`,
        {
          method: 'GET',
          cache: 'no-store',
          credentials: 'omit',
          redirect: 'error',
          signal: controller.signal,
          headers: { accept: 'application/json' },
        },
      );
      if (!response.ok) throw new Error('Fleet board detail is unavailable');
      const bytes = Buffer.from(await response.arrayBuffer());
      if (bytes.length > MAX_FLEET_BODY_BYTES) throw new Error('Fleet board detail is too large');
      return JSON.parse(bytes.toString('utf8'));
    } finally {
      clearTimeout(timeout);
    }
  };
}

function readBody(request) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    let complete = false;
    const finish = (callback, value) => {
      if (complete) return;
      complete = true;
      request.removeListener('data', onData);
      request.removeListener('end', onEnd);
      request.removeListener('error', onError);
      callback(value);
    };
    const onData = (chunk) => {
      size += chunk.length;
      if (size > MAX_BODY_BYTES) {
        finish(reject, Object.assign(new Error('request body is too large'), { statusCode: 413 }));
        request.resume();
        return;
      }
      chunks.push(chunk);
    };
    const onEnd = () => finish(resolve, Buffer.concat(chunks));
    const onError = (error) => finish(reject, error);
    request.on('data', onData);
    request.on('end', onEnd);
    request.on('error', onError);
  });
}

function addCors(response, origin) {
  response.setHeader('access-control-allow-origin', origin);
  response.setHeader('access-control-allow-methods', 'GET, POST, OPTIONS');
  response.setHeader('access-control-allow-headers', `content-type, ${TOKEN_HEADER}`);
  response.setHeader('access-control-max-age', '300');
  response.setHeader('vary', 'Origin');
}

function json(response, status, body, origin) {
  addCors(response, origin);
  response.writeHead(status, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' });
  response.end(JSON.stringify(body));
}

function createHelperServer(options) {
  const board = String(options.board || '');
  if (!SAFE_BOARD.test(board)) fail('board must be a safe 1-80 character identifier');
  const origin = normalizeOrigin(options.origin);
  const token = String(options.token || '');
  if (token.length < 32 || token.length > 512 || /\s/.test(token)) fail('token must contain 32-512 characters without whitespace');
  const host = options.host || '127.0.0.1';
  if (!isLoopbackHostname(host)) fail('helper host must be loopback');
  const port = options.port === undefined ? DEFAULT_PORT : parseInteger(options.port, 'port', 0, 65535);
  const runBridge = options.runBridge || createBridgeRunner(options.bridgeCommand || 'pursers-wait-bridge', options.bridgeStateDir);
  const runTeamCli = options.runTeamCli || createTeamRunner(options.aioncoreCommand || 'aioncore');
  const ticketLifecycle = options.ticketLifecycle || createTicketLifecycleProcess({
    command: options.bridgeCommand || 'pursers-wait-bridge',
    stateDir: options.bridgeStateDir,
    board,
  });
  const fetchResults = options.fetchResults || createFleetResultsFetcher(
    options.fleetUrl || DEFAULT_FLEET_URL,
    options.fetchImpl,
  );
  const handlers = createHandlers({
    allowedOrigin: origin,
    expectedBoard: board,
    runBridge,
    runTeamCli,
    runTicketLifecycle: ticketLifecycle.run,
    fetchResults,
    importMcp: async () => ({ success: true, imported: false }),
  });

  const server = http.createServer(async (request, response) => {
    const requestOrigin = request.headers.origin || '';
    if (requestOrigin !== origin) {
      response.writeHead(403, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' });
      response.end(JSON.stringify({ ok: false, error: 'origin_not_allowed' }));
      return;
    }
    if (request.method === 'OPTIONS') {
      addCors(response, origin);
      response.writeHead(204, { 'cache-control': 'no-store' });
      response.end();
      return;
    }
    if (!safeEqual(request.headers[TOKEN_HEADER], token)) {
      json(response, 401, { ok: false, error: 'helper_auth_failed' }, origin);
      return;
    }
    const incomingUrl = new URL(request.url || '/', `http://${host}:${server.address()?.port || port}`);
    if (!incomingUrl.pathname.startsWith('/pursers/')) {
      json(response, 404, { ok: false, error: 'not_found' }, origin);
      return;
    }
    if (request.method === 'GET' && incomingUrl.pathname === '/pursers/helper/status') {
      json(response, 200, {
        ok: true,
        board,
        transport: 'authenticated_loopback_helper',
        host_route_handlers: false,
        team_context: 'unavailable_from_settings_tab',
        core_version: options.coreVersion || 'unknown',
      }, origin);
      return;
    }
    try {
      const body = ['GET', 'HEAD'].includes(request.method || '') ? undefined : await readBody(request);
      const headers = new Headers();
      for (const [name, value] of Object.entries(request.headers)) {
        if (Array.isArray(value)) headers.set(name, value.join(', '));
        else if (value !== undefined) headers.set(name, value);
      }
      const routed = await handlers.handle(new Request(incomingUrl, {
        method: request.method,
        headers,
        body: body && body.length ? body : undefined,
      }));
      addCors(response, origin);
      for (const [name, value] of routed.headers.entries()) response.setHeader(name, value);
      response.setHeader('cache-control', 'no-store');
      response.writeHead(routed.status);
      response.end(Buffer.from(await routed.arrayBuffer()));
    } catch (error) {
      json(response, error.statusCode || 500, { ok: false, error: error.statusCode === 413 ? 'request_too_large' : 'helper_failure' }, origin);
    }
  });

  return {
    server,
    async start() {
      await new Promise((resolve, reject) => {
        server.once('error', reject);
        server.listen(port, host, resolve);
      });
      return server.address();
    },
    async close() {
      if (server.listening) {
        await new Promise((resolve, reject) => server.close((error) => (error ? reject(error) : resolve())));
      }
      await ticketLifecycle.close();
    },
  };
}

function parseArgs(argv) {
  const values = {};
  for (let index = 0; index < argv.length; index += 2) {
    const name = argv[index];
    const value = argv[index + 1];
    if (!name?.startsWith('--') || value === undefined) fail('arguments must be --name value pairs');
    values[name.slice(2)] = value;
  }
  for (const required of ['board', 'origin', 'token-file', 'bridge-state-dir']) {
    if (!values[required]) fail(`--${required} is required`);
  }
  return values;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const token = readTokenFile(args['token-file']);
  const helper = createHelperServer({
    board: args.board,
    origin: args.origin,
    token,
    host: args.host || '127.0.0.1',
    port: args.port === undefined ? DEFAULT_PORT : parseInteger(args.port, '--port', 0, 65535),
    bridgeCommand: args['bridge-bin'] || 'pursers-wait-bridge',
    bridgeStateDir: args['bridge-state-dir'],
    aioncoreCommand: args['aioncore-bin'] || 'aioncore',
    fleetUrl: args['fleet-url'] || DEFAULT_FLEET_URL,
    coreVersion: args['core-version'] || 'unknown',
  });
  const address = await helper.start();
  process.stdout.write(`${JSON.stringify({ ok: true, host: address.address, port: address.port, board: args.board, origin: normalizeOrigin(args.origin) })}\n`);
  const stop = async () => {
    await helper.close();
    process.exit(0);
  };
  process.on('SIGINT', stop);
  process.on('SIGTERM', stop);
}

module.exports = {
  DEFAULT_PORT,
  DEFAULT_FLEET_URL,
  MAX_BODY_BYTES,
  TOKEN_HEADER,
  bridgeArguments,
  createFleetResultsFetcher,
  createHelperServer,
  normalizeOrigin,
  readTokenFile,
};

if (require.main === module) {
  main().catch((error) => {
    process.stderr.write(`pursers-home-helper: ${error.message}\n`);
    process.exitCode = 1;
  });
}
