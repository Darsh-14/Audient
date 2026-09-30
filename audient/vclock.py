"""Virtual-time asyncio event loop.

Idle waiting (e.g. a mock tool's 1.5 s latency) is skipped instantly, while real
CPU time -- agent callbacks on the loop thread and heavy perception work pushed
through :func:`compute` (OCR / ASR in a thread) -- is charged to the virtual
clock. Measured latencies are therefore honest, yet a 20 s scenario replays in
milliseconds when the agent is not doing heavy compute.
"""
from __future__ import annotations

import asyncio
import selectors
import time
from typing import Any, Callable


class VirtualClock:
    def __init__(self) -> None:
        self.t = 0.0
        self.busy = 0  # executor jobs in flight -> clock must follow wall time


class _VirtualSelector(selectors.SelectSelector):
    def __init__(self, clock: VirtualClock) -> None:
        super().__init__()
        self._clock = clock
        self._last = time.perf_counter()

    def select(self, timeout=None):
        now = time.perf_counter()
        self._clock.t += now - self._last  # charge CPU time spent running callbacks
        if self._clock.busy > 0:
            ready = super().select(timeout)
            after = time.perf_counter()
            self._clock.t += after - now  # real waiting while compute runs
        else:
            ready = super().select(0)
            if not ready:
                if timeout is None:
                    ready = super().select(0.01)
                elif timeout > 0:
                    self._clock.t += timeout  # jump over idle time
            after = time.perf_counter()
        self._last = after
        return ready


class VirtualEventLoop(asyncio.SelectorEventLoop):
    def __init__(self, clock: VirtualClock | None = None) -> None:
        self.clock = clock or VirtualClock()
        super().__init__(_VirtualSelector(self.clock))

    def time(self) -> float:
        return self.clock.t


async def compute(fn: Callable[..., Any], *args: Any) -> Any:
    """Run blocking work in a thread; on a virtual loop its wall time is charged."""
    loop = asyncio.get_running_loop()
    clock = getattr(loop, "clock", None)
    if clock is not None:
        clock.busy += 1
    try:
        return await loop.run_in_executor(None, fn, *args)
    finally:
        if clock is not None:
            clock.busy -= 1
