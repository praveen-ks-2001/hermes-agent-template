import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from starlette.responses import JSONResponse
from starlette.testclient import TestClient

import server


class EmptyAsyncStdout:
    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class FakeProc:
    def __init__(self, pid: int, returncode: int | None = None):
        self.pid = pid
        self.returncode = returncode
        self.stdout = EmptyAsyncStdout()


class GatewaySyncTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = TestClient(server.app)
        self.client.cookies.set(server.COOKIE_NAME, server._make_auth_token())

    def test_native_dashboard_restart_uses_wrapper_gateway_manager(self):
        proxied = JSONResponse({"proxied": True}, status_code=599)

        with (
            patch.object(server.gw, "restart", new=AsyncMock()) as restart,
            patch.object(server, "_proxy_to_dashboard", new=AsyncMock(return_value=proxied)) as proxy,
        ):
            response = self.client.post("/api/gateway/restart")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True, "name": "gateway-restart"})
        restart.assert_called_once()
        proxy.assert_not_called()

    async def test_old_gateway_drain_does_not_mark_new_process_as_error(self):
        gateway = server.Gateway()
        old_proc = FakeProc(pid=100, returncode=0)
        new_proc = FakeProc(pid=200, returncode=None)

        gateway.proc = new_proc
        gateway.state = "running"

        await gateway._drain(old_proc)

        self.assertIs(gateway.proc, new_proc)
        self.assertEqual(gateway.state, "running")
        self.assertEqual(list(gateway.logs), [])

    async def test_start_adopts_existing_gateway_pid_instead_of_spawning_duplicate(self):
        sleep_proc = await asyncio.create_subprocess_exec("sleep", "30")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                pid_file = Path(tmp) / "gateway.pid"
                pid_file.write_text(str(sleep_proc.pid))
                gateway = server.Gateway()

                with (
                    patch.object(server, "GATEWAY_PID_FILE", pid_file),
                    patch("server.asyncio.create_subprocess_exec", new=AsyncMock()) as spawn,
                ):
                    await gateway.start()
                    status = gateway.status()

            spawn.assert_not_called()
            self.assertEqual(status["state"], "running")
            self.assertEqual(status["pid"], sleep_proc.pid)
        finally:
            sleep_proc.terminate()
            await sleep_proc.wait()


if __name__ == "__main__":
    unittest.main()
