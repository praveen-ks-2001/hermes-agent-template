import asyncio
import importlib.util
import json
import os
import re
import tomllib
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


os.environ.setdefault("ADMIN_PASSWORD", "test-password")
import server


ROOT = Path(server.__file__).parent


def load_build_hardener():
    path = ROOT / "build_tools" / "harden_hermes.py"
    if not path.exists():
        raise AssertionError(f"missing build-time Hermes hardener: {path}")
    spec = importlib.util.spec_from_file_location("harden_hermes", path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"could not load build-time Hermes hardener: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HermesPidOneGuardTests(unittest.TestCase):
    def test_guard_matches_pid_one_signal_commands_at_command_positions(self):
        hardener = load_build_hardener()
        guard = re.compile(hardener.PID1_PATTERN, re.IGNORECASE | re.DOTALL)

        blocked = (
            "kill 1",
            "kill -TERM 1",
            "kill -15 1",
            "kill -s TERM 1",
            "kill --signal=KILL 1",
            "/bin/kill -TERM 1",
            "sudo /usr/bin/kill -TERM 1",
            "echo ready && kill -TERM 2 1",
            "$(kill -TERM '1')",
        )
        for command in blocked:
            with self.subTest(command=command):
                self.assertIsNotNone(guard.search(command))

    def test_guard_does_not_block_pid_prefixes_or_command_text_used_as_data(self):
        hardener = load_build_hardener()
        guard = re.compile(hardener.PID1_PATTERN, re.IGNORECASE | re.DOTALL)

        allowed = (
            "kill -TERM 10",
            "kill -TERM 11",
            "echo 'kill -TERM 1'",
            "printf '%s' 'kill 1'",
            "gh issue create --body 'kill -TERM 1'",
        )
        for command in allowed:
            with self.subTest(command=command):
                self.assertIsNone(guard.search(command))

    def test_build_patch_is_idempotent_and_fails_closed_on_upstream_drift(self):
        hardener = load_build_hardener()
        source = "HARDLINE_PATTERNS = [\n" + hardener.UPSTREAM_ANCHOR + "]\n"

        once = hardener.patch_source(source)
        twice = hardener.patch_source(once)

        self.assertEqual(once, twice)
        self.assertEqual(once.count(hardener.PID1_DESCRIPTION), 1)
        with self.assertRaisesRegex(RuntimeError, "anchor"):
            hardener.patch_source("HARDLINE_PATTERNS = []\n")


class CriticalProcessHealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_is_unavailable_while_dashboard_is_down(self):
        dashboard_status = {
            "state": "restarting",
            "pid": None,
            "uptime": None,
            "restarts": 2,
        }
        with patch.object(server.dash, "status", return_value=dashboard_status):
            response = await server.route_health(None)

        body = json.loads(response.body)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(body["status"], "degraded")
        self.assertEqual(body["dashboard"], dashboard_status)

    async def test_health_is_available_when_dashboard_is_running(self):
        dashboard_status = {
            "state": "running",
            "pid": 123,
            "uptime": 5,
            "restarts": 0,
        }
        with patch.object(server.dash, "status", return_value=dashboard_status):
            response = await server.route_health(None)

        body = json.loads(response.body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["gateway"], server.gw.state)

    async def test_unexpected_dashboard_exit_queues_a_respawn(self):
        class EmptyOutput:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

        class ExitedProcess:
            pid = 456
            returncode = -15
            stdout = EmptyOutput()

            async def wait(self):
                return self.returncode

        dashboard = server.Dashboard()
        process = ExitedProcess()
        dashboard.proc = process
        dashboard.state = "running"

        with patch.object(dashboard, "_queue_respawn", new=Mock()) as queue:
            await dashboard._drain(process)

        self.assertEqual(dashboard.state, "error")
        queue.assert_called_once_with()

    async def test_deliberate_dashboard_stop_does_not_queue_a_respawn(self):
        class EmptyOutput:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

        class ExitedProcess:
            pid = 789
            returncode = 0
            stdout = EmptyOutput()

            async def wait(self):
                return self.returncode

        dashboard = server.Dashboard()
        process = ExitedProcess()
        dashboard.proc = process
        dashboard.state = "stopping"
        dashboard._stopping = True

        with patch.object(dashboard, "_queue_respawn", new=Mock()) as queue:
            await dashboard._drain(process)

        queue.assert_not_called()

    async def test_dashboard_supervisor_restarts_after_backoff(self):
        class DeadProcess:
            pid = 111
            returncode = 1

        class LiveProcess:
            pid = 222
            returncode = None

        dashboard = server.Dashboard()
        dashboard.proc = DeadProcess()
        dashboard.state = "error"

        async def make_live(*, reset_budget):
            self.assertFalse(reset_budget)
            dashboard.proc = LiveProcess()
            dashboard.state = "running"

        with (
            patch.object(asyncio, "sleep", return_value=None) as sleep,
            patch.object(dashboard, "start", side_effect=make_live) as start,
        ):
            await dashboard._supervise_respawn()

        sleep.assert_awaited_once_with(server.RESPAWN_BASE_DELAY)
        start.assert_awaited_once_with(reset_budget=False)
        self.assertEqual(dashboard.restarts, 1)
        self.assertEqual(dashboard.state, "running")


class RailwayLifecyclePolicyTests(unittest.TestCase):
    def test_clean_exit_is_restarted_and_dockerfile_owns_the_entrypoint(self):
        config = tomllib.loads((ROOT / "railway.toml").read_text())
        deploy = config["deploy"]

        self.assertEqual(deploy["restartPolicyType"].lower(), "always")
        self.assertNotIn("startCommand", deploy)


if __name__ == "__main__":
    unittest.main()
