import assert from 'node:assert/strict'
import { once } from 'node:events'
import { test } from 'node:test'
import { setTimeout as delay } from 'node:timers/promises'
import { localRoute, ownedProcess } from './runner-support.mjs'

test('page routes cannot fetch or mock a matching path at another origin', async () => {
  const origin = 'http://127.0.0.1:43210'
  for (const destination of [
    'http://127.0.0.1:43211', 'http://localhost:43210', 'https://127.0.0.1:43210',
  ]) for (const path of ['/api/reimbursements/drafts/synthetic', '/files/synthetic/preview/pages/1']) {
    let guarded = false
    let fetched = false
    const route = { request: () => ({ url: () => destination + path }), fallback: () => { guarded = true } }
    await localRoute(origin, () => { fetched = true })(route)
    assert(guarded)
    assert.equal(fetched, false)
  }
  const route = { request: () => ({ url: () => origin + '/api/reimbursements/drafts/synthetic' }),
    fallback: () => { throw new Error('Local fixture was incorrectly rejected') } }
  assert.equal(await localRoute(origin, () => 'local response')(route), 'local response')
})

test('forced shutdown closes descendants that ignore TERM and inherit pipes', { timeout: 10_000 }, async () => {
  const descendant = `
    const http = require('node:http');
    process.on('SIGTERM', () => {});
    const server = http.createServer((_request, response) => response.end('synthetic'));
    server.listen(0, '127.0.0.1', () => process.stdout.write(JSON.stringify({ port: server.address().port }) + '\\n'));
  `
  const wrapper = `
    const { spawn } = require('node:child_process');
    process.on('SIGTERM', () => {});
    spawn(process.execPath, ['-e', ${JSON.stringify(descendant)}], { stdio: ['ignore', process.stdout, process.stderr] });
  `
  const owned = ownedProcess(process.execPath, ['-e', wrapper], { stdio: ['ignore', 'pipe', 'pipe'] })
  try {
    const [line] = await Promise.race([
      once(owned.child.stdout, 'data'),
      delay(2000, undefined, { ref: false }).then(() => {
        throw new Error('Synthetic descendant did not start')
      }),
    ])
    const { port } = JSON.parse(line.toString())
    assert((await fetch(`http://127.0.0.1:${port}`)).ok)
    await owned.stop(100)
    await assert.rejects(fetch(`http://127.0.0.1:${port}`, { signal: AbortSignal.timeout(500) }))
  } finally { await owned.stop(100) }
})
