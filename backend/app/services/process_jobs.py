from __future__ import annotations

import asyncio
import multiprocessing
from collections.abc import Awaitable, Callable
from multiprocessing.connection import Connection
from typing import Any

import anyio


class ProcessJobBusy(RuntimeError):
    """The single local native-processing slot is already in use."""


class ProcessJobTimeout(RuntimeError):
    """A process job exceeded its deadline and its worker was discarded."""


class ProcessJobResourceLimit(RuntimeError):
    """The isolated native worker failed or exited under an OS resource limit."""


class _FreshProcessFailure(RuntimeError):
    """Internal marker for a child that failed without returning a safe result."""


ProcessRunSync = Callable[..., Awaitable[Any]]


def _fresh_process_entry(
    connection: Connection,
    function: Callable[..., Any],
    args: tuple[object, ...],
) -> None:
    """Run exactly one trusted entry point and return no exception details."""

    try:
        try:
            result = function(*args)
            payload: tuple[str, object] = ("ok", result)
        except BaseException as exc:
            payload = ("error", type(exc).__name__)
        try:
            connection.send(payload)
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        connection.close()


async def _wait_for_descriptor(descriptor: int) -> None:
    loop = asyncio.get_running_loop()
    ready = loop.create_future()

    def mark_ready() -> None:
        if not ready.done():
            ready.set_result(None)

    try:
        loop.add_reader(descriptor, mark_ready)
    except (AttributeError, NotImplementedError) as exc:
        raise _FreshProcessFailure("async process monitoring is unavailable") from exc
    try:
        await ready
    finally:
        loop.remove_reader(descriptor)


async def _wait_for_process_exit(process: multiprocessing.Process) -> None:
    if process.exitcode is not None:
        process.join()
        return
    await _wait_for_descriptor(process.sentinel)
    process.join()


async def _terminate_process(process: multiprocessing.Process) -> None:
    """Terminate, escalate if needed, and reap a started child process."""

    if process.exitcode is not None:
        process.join()
        return

    async def wait_bounded() -> bool:
        try:
            await asyncio.wait_for(_wait_for_process_exit(process), timeout=2)
        except _FreshProcessFailure:
            # Proactor-style loops may not implement add_reader. The child has
            # already received a termination signal, so a documented bounded
            # multiprocessing join is the fail-closed fallback.
            process.join(timeout=2)
        except TimeoutError:
            return False
        return process.exitcode is not None

    process.terminate()
    if await wait_bounded():
        return
    process.kill()
    if not await wait_bounded():
        # SIGKILL/TerminateProcess cannot be handled by the child. Joining here
        # guarantees that admission is never released ahead of OS reaping.
        process.join()


async def _shield_process_cleanup(process: multiprocessing.Process) -> None:
    """Reap and close despite repeated caller cancellation, then re-raise it."""

    async def finalize() -> None:
        if process.exitcode is None:
            await _terminate_process(process)
        process.join()
        process.close()

    cleanup = asyncio.create_task(finalize())
    cancellation: asyncio.CancelledError | None = None
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
    cleanup_error: BaseException | None = None
    try:
        await cleanup
    except BaseException as exc:
        cleanup_error = exc
    if cancellation is not None:
        raise cancellation from cleanup_error
    if cleanup_error is not None:
        raise cleanup_error


async def run_in_fresh_process(
    function: Callable[..., Any],
    *args: object,
    cancellable: bool,
    limiter: anyio.CapacityLimiter,
) -> Any:
    """Run one job in a fresh stdlib spawn process and always reap it.

    The keyword arguments retain the former injected runner seam. Admission is
    owned by ``KillableProcessRunner``; a new OS process provides profile
    isolation so an OCR worker can never be reused for file validation.
    """

    del cancellable, limiter
    context = multiprocessing.get_context("spawn")
    receiving, sending = context.Pipe(duplex=False)
    process = context.Process(
        target=_fresh_process_entry,
        args=(sending, function, args),
    )
    started = False
    result: Any = None
    primary_error: BaseException | None = None
    try:
        try:
            try:
                process.start()
                started = True
            except (OSError, RuntimeError, TypeError) as exc:
                raise _FreshProcessFailure("isolated process could not start") from exc
            finally:
                sending.close()

            await _wait_for_descriptor(receiving.fileno())
            try:
                kind, payload = receiving.recv()
            except (EOFError, OSError, TypeError, ValueError) as exc:
                raise _FreshProcessFailure("isolated process returned no result") from exc
            await _wait_for_process_exit(process)
            if kind != "ok":
                raise _FreshProcessFailure(f"isolated worker failed: {payload}")
            result = payload
        except BaseException as exc:
            primary_error = exc
    finally:
        receiving.close()
        sending.close()
        cleanup_error: BaseException | None = None
        if started:
            try:
                await _shield_process_cleanup(process)
            except BaseException as exc:
                cleanup_error = exc
        if primary_error is not None:
            raise primary_error.with_traceback(primary_error.__traceback__)
        if cleanup_error is not None:
            raise cleanup_error
    return result


class KillableProcessRunner:
    """Admit one local job per runner in a fresh, killable process.

    Callers may use a bounded FIFO admission queue. Every admitted job receives
    a new process, preventing the 5 GiB OCR profile (and its retained model
    state) from crossing into the 512 MiB image/PDF validation profile.

    The application may give upload validation its own one-slot runner, so
    lightweight bounded checks can overlap OCR without adding an OCR slot.
    """

    def __init__(
        self,
        run_sync: ProcessRunSync | None = None,
        *,
        admission_timeout_seconds: float = 1.0,
        max_waiters: int | None = None,
    ) -> None:
        if admission_timeout_seconds <= 0:
            raise ValueError("admission_timeout_seconds must be positive")
        if max_waiters is not None and (
            isinstance(max_waiters, bool) or not isinstance(max_waiters, int) or max_waiters < 0
        ):
            raise ValueError("max_waiters must be a non-negative integer or None")
        self._run_sync = run_sync or run_in_fresh_process
        self._admission = anyio.CapacityLimiter(1)
        self._process_capacity = anyio.CapacityLimiter(1)
        self._active_task: asyncio.Task[Any] | None = None
        self._run_tasks: set[asyncio.Task[Any]] = set()
        self._closing = False
        self._admission_timeout_seconds = admission_timeout_seconds
        self._max_waiters = max_waiters
        self._waiting_jobs = 0

    @property
    def waiting_jobs(self) -> int:
        return self._waiting_jobs

    async def _acquire_admission(self) -> None:
        try:
            self._admission.acquire_nowait()
            return
        except anyio.WouldBlock:
            pass

        if self._max_waiters is not None and self._waiting_jobs >= self._max_waiters:
            raise ProcessJobBusy("local process queue is full")
        self._waiting_jobs += 1
        try:
            with anyio.move_on_after(self._admission_timeout_seconds):
                await self._admission.acquire()
                return
            raise ProcessJobBusy("local process queue wait timed out")
        finally:
            self._waiting_jobs -= 1

    async def run(
        self,
        function: Callable[..., Any],
        *args: object,
        timeout_seconds: float,
    ) -> Any:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("process job requires an asyncio task")
        self._run_tasks.add(task)
        admitted = False
        try:
            if self._closing:
                raise ProcessJobBusy("process runner is closing")
            await self._acquire_admission()
            admitted = True
            if self._closing:
                raise ProcessJobBusy("process runner is closing")

            self._active_task = task
            try:
                with anyio.fail_after(timeout_seconds):
                    return await self._run_sync(
                        function,
                        *args,
                        cancellable=True,
                        limiter=self._process_capacity,
                    )
            except TimeoutError as exc:
                raise ProcessJobTimeout("local process job timed out") from exc
            except _FreshProcessFailure as exc:
                raise ProcessJobResourceLimit("local process worker exited") from exc
        finally:
            if self._active_task is task:
                self._active_task = None
            if admitted:
                self._admission.release()
            self._run_tasks.discard(task)

    async def close(self, timeout_seconds: float = 5.0) -> None:
        """Stop active/waiting jobs and do not return before their cleanup."""

        self._closing = True
        current = asyncio.current_task()
        targets = [task for task in self._run_tasks if task is not current and not task.done()]
        for task in targets:
            task.cancel()
        if not targets:
            return

        async def finish_targets() -> None:
            done, pending = await asyncio.wait(targets, timeout=timeout_seconds)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            if done:
                await asyncio.gather(*done, return_exceptions=True)

        cleanup = asyncio.create_task(finish_targets())
        cancellation: asyncio.CancelledError | None = None
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError as exc:
                cancellation = cancellation or exc
        await cleanup
        if cancellation is not None:
            raise cancellation
