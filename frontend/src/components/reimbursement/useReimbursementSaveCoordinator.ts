import { onScopeDispose, readonly, ref } from 'vue'

interface SaveTarget {
  scope: string
  draftId: string | undefined
}

interface SaveCoordinatorOptions {
  target: () => SaveTarget
  canAutosave: () => boolean
  autosave: () => Promise<void>
}

/** Serializes saves for one editing scope and owns its background save timer. */
export function useReimbursementSaveCoordinator(options: SaveCoordinatorOptions) {
  const pending = ref(false)
  let timer: ReturnType<typeof setTimeout> | undefined
  let active: Promise<void> | null = null
  let generation = 0
  let disposed = false

  function accepts(target: SaveTarget, expectedGeneration: number): boolean {
    const current = options.target()
    return !disposed && generation === expectedGeneration
      && target.scope === current.scope && target.draftId === current.draftId
  }

  function cancelScheduled(): void {
    if (timer !== undefined) clearTimeout(timer)
    timer = undefined
  }

  function schedule(): void {
    cancelScheduled()
    if (disposed || !options.canAutosave()) return
    const target = { ...options.target() }
    const expectedGeneration = generation
    timer = setTimeout(() => {
      timer = undefined
      if (!accepts(target, expectedGeneration) || !options.canAutosave()) return
      // The caller owns the error state and the explicit retry action.
      void options.autosave().catch(() => undefined)
    }, 600)
  }

  async function run<T>(operation: () => Promise<T>): Promise<T> {
    cancelScheduled()
    const target = { ...options.target() }
    const expectedGeneration = generation
    while (true) {
      if (!accepts(target, expectedGeneration)) {
        throw new Error('当前报销已变化，原保存操作已取消')
      }
      if (!active) break
      // A rejected save must still release the queue for an explicit recovery.
      await active
    }
    const running = operation()
    const lock = running.then(() => undefined, () => undefined)
    active = lock
    pending.value = true
    try {
      return await running
    } finally {
      // A prior scope can finish after its successor has already started saving.
      if (active === lock) {
        active = null
        pending.value = false
      }
    }
  }

  function reset(): void {
    cancelScheduled()
    generation += 1
    active = null
    pending.value = false
  }

  onScopeDispose(() => {
    disposed = true
    reset()
  })

  return { pending: readonly(pending), schedule, cancelScheduled, run, reset }
}
