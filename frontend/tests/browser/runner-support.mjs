import { spawn } from 'node:child_process'
import { setTimeout as delay } from 'node:timers/promises'

export function localRoute(origin, handler) {
  return route => new URL(route.request().url()).origin === origin
    ? handler(route) : route.fallback()
}

export function ownedProcess(command, args, options) {
  if (process.platform === 'win32') throw new Error('Browser smoke requires macOS or Linux')
  const child = spawn(command, args, { ...options, detached: true })
  const exited = new Promise(resolve => child.once('close', resolve))
  function signalGroup(signal) {
    if (!child.pid) return
    try { process.kill(-child.pid, signal) }
    catch (error) { if (error.code !== 'ESRCH') throw error }
  }
  async function stop(graceMs = 5000) {
    signalGroup('SIGTERM')
    const stopped = await Promise.race([
      exited.then(() => true), delay(graceMs, undefined, { ref: false }).then(() => false),
    ])
    if (!stopped) signalGroup('SIGKILL')
    await Promise.race([
      exited,
      delay(5000, undefined, { ref: false }).then(() => {
        throw new Error('Synthetic server process group did not stop')
      }),
    ])
  }
  return { child, stop }
}
