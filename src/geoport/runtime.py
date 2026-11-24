# Hand-repaired from GeoPort v5.0.3 bytecode (see docs/RECOVERY.md).
"""Persistent background asyncio event-loop runtime for GeoPort v5.

CRITICAL ARCHITECTURAL INVARIANT:
pymobiledevice3 v11.61.0's UserspaceRsdTunnel (backed by pmd-pytcp) creates asyncio
streams, UDP socketpair relays (UserspaceDialPlane), and background tasks bound to a
single running event loop. Furthermore, DvtProvider and LocationSimulation must stay
open on that exact same event loop for iOS 17+ to hold simulated GPS coordinates.

Flask routes run on synchronous worker threads, so AsyncRuntime owns one dedicated
daemon event-loop thread for the lifetime of the application and dispatches all
async coroutines onto it via asyncio.run_coroutine_threadsafe().
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import sys
import threading
from typing import Any, Coroutine, Optional, TypeVar

from geoport.config import logger

T = TypeVar('T')


def ensure_platform_event_loop_policy() -> None:
    """Ensure Windows uses SelectorEventLoopPolicy (required by UserspaceTun sock_recv)."""
    if sys.platform == 'win32':
        policy_cls = getattr(asyncio, 'WindowsSelectorEventLoopPolicy', None)
        if policy_cls is not None and not isinstance(asyncio.get_event_loop_policy(), policy_cls):
            asyncio.set_event_loop_policy(policy_cls())


class AsyncRuntime:
    """Thread-safe persistent asyncio event-loop runner."""

    def __init__(self, name: str = 'geoport-async-runtime') -> None:
        ensure_platform_event_loop_policy()
        self._name = name
        self._loop = None
        self._thread = None
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._closed = False

    def start(self) -> asyncio.AbstractEventLoop:
        """Start the background event-loop thread if not already running."""
        with self._lock:
            if self._loop is not None and self._loop.is_running():
                return self._loop
            self._closed = False
            self._ready.clear()
            self._thread = threading.Thread(target=self._thread_main, name=self._name, daemon=True)
            self._thread.start()
        if not self._ready.wait(timeout=10.0):
            raise RuntimeError('Timed out starting GeoPort AsyncRuntime event loop')
        assert self._loop is not None
        return self._loop

    def _thread_main(self) -> None:
        ensure_platform_event_loop_policy()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            try:
                pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception as exc:
                logger.debug(f'AsyncRuntime shutdown cleanup error: {exc}')
            finally:
                loop.close()

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        return self.start()

    def run(self, coro: Coroutine[Any, Any, T], timeout: Optional[float] = 60.0) -> T:
        """Execute ``coro`` on the persistent event loop and return its result synchronously."""
        loop = self.start()
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is loop:
            raise RuntimeError('AsyncRuntime.run() must not be called from inside its own event loop; use await')
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise TimeoutError(f'Async operation timed out after {timeout}s') from exc

    def submit(self, coro: Coroutine[Any, Any, T]) -> concurrent.futures.Future[T]:
        """Schedule ``coro`` on the persistent event loop without blocking."""
        loop = self.start()
        return asyncio.run_coroutine_threadsafe(coro, loop)

    def shutdown(self, timeout: float = 5.0) -> None:
        """Stop the background event loop cleanly."""
        with self._lock:
            if self._closed or self._loop is None:
                return
            self._closed = True
            loop = self._loop
            thread = self._thread
        if loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)


_default_runtime: Optional[AsyncRuntime] = None
_default_runtime_lock = threading.Lock()


def get_runtime() -> AsyncRuntime:
    """Return the singleton process-wide AsyncRuntime."""
    global _default_runtime
    with _default_runtime_lock:
        if _default_runtime is None:
            _default_runtime = AsyncRuntime()
            _default_runtime.start()
        return _default_runtime
