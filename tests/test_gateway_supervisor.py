import asyncio
import contextlib
import sys
import time
import unittest
from unittest.mock import AsyncMock

from server import Gateway


class GatewaySupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def test_unexpected_exit_is_detected_when_descendant_holds_stdout_open(self):
        """A dead gateway must respawn even if an orphan keeps its stdout pipe open."""
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            (
                "import subprocess, sys; "
                "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(1.5)']); "
                "print('gateway parent exiting', flush=True)"
            ),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        gateway = Gateway()
        gateway.proc = proc
        gateway.state = "running"
        gateway.started_at = time.time()
        gateway._started_monotonic = time.monotonic()
        respawn_scheduled = asyncio.Event()

        async def record_respawn(_pid):
            respawn_scheduled.set()

        gateway._supervise_respawn = AsyncMock(side_effect=record_respawn)

        drain_task = asyncio.create_task(gateway._drain(proc))
        try:
            await asyncio.wait_for(respawn_scheduled.wait(), timeout=0.6)
        finally:
            drain_task.cancel()
            await asyncio.gather(drain_task, return_exceptions=True)
            # Let the fixture descendant exit and release the inherited pipe so
            # asyncio can close its subprocess transport before the test loop.
            if proc.stdout:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(proc.stdout.read(), timeout=2.0)
            await proc.wait()

        gateway._supervise_respawn.assert_awaited_once_with(proc.pid)
        self.assertEqual(gateway.state, "error")


if __name__ == "__main__":
    unittest.main()
