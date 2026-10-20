import base64
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("stream_tools", ROOT / "tools.py")
tools = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tools)
spec = importlib.util.spec_from_file_location("stream_schema", ROOT / "schemas.py")
schemas = importlib.util.module_from_spec(spec)
spec.loader.exec_module(schemas)


class StreamingReviewTests(unittest.TestCase):
    def test_injections_rejected_before_any_effect(self):
        cases = [
            {"host": "localhost; echo injected"},
            {"host": "-oProxyCommand=echo"},
            {"host": "$(echo injected)"},
            {"ssh_user": "-oProxyCommand=echo", "ssh_host": "localhost"},
            {"ssh_user": "user", "ssh_host": "-oProxyCommand=echo"},
            {"ssh_user": "user"},
            {"ssh_host": "localhost"},
            {"resolution": "1920x1080; echo injected"},
            {"resolution": "1920x1080\n"},
            {"apps": ["--config=untrusted"]},
            {"client_name": "client\nPY\nprint('injected')"},
            {"role": "anything"},
        ]
        with patch.object(tools, "_run") as run, patch.object(tools, "_probe_ports") as probe, \
             patch.object(tools.subprocess, "Popen") as popen:
            for payload in cases:
                with self.subTest(payload=payload):
                    result = json.loads(tools.game_stream_handler({"action": "launch", **payload}))
                    self.assertFalse(result["success"])
            run.assert_not_called()
            probe.assert_not_called()
            popen.assert_not_called()

    def test_ssh_option_terminator_and_ipv6(self):
        with patch.object(tools, "_run", return_value=(0, "ok")) as run:
            tools._ssh("user", "2001:db8::1", "id -u")
        self.assertEqual(run.call_args.args[0][-3:], ["--", "user@2001:db8::1", "id -u"])
        with patch.object(tools, "_run") as run:
            with self.assertRaises(ValueError):
                tools._ssh("-oProxyCommand=echo", "localhost", "id")
            run.assert_not_called()

    def test_launch_uses_literal_argv_and_environment(self):
        app = "Desktop'; echo synthetic; #"
        env_value = ":0'; echo synthetic; #"
        proc = Mock(pid=123)
        proc.poll.return_value = None
        with patch.object(tools, "_probe_ports", return_value={p: "OPEN" for p in tools.STREAM_PORTS}), \
             patch.object(tools, "_run", side_effect=[(0, "/usr/bin/moonlight"), (0, "123")]), \
             patch.object(tools.Path, "read_bytes", return_value=("DISPLAY="+env_value+"\0IGNORED=x\0").encode()), \
             patch.object(tools.subprocess, "Popen", return_value=proc) as popen, \
             patch.object(tools.time, "sleep"):
            result = json.loads(tools.game_stream_handler(
                {"action": "launch", "host": "localhost", "resolution": "1920x1080@60", "apps": [app]}))
        self.assertTrue(result["success"])
        self.assertEqual(popen.call_args.args[0], ["moonlight", "stream", "-res", "1920x1080@60", "localhost", app])
        self.assertFalse(popen.call_args.kwargs.get("shell", False))
        self.assertEqual(popen.call_args.kwargs["env"]["DISPLAY"], env_value)
        self.assertNotIn("IGNORED", popen.call_args.kwargs["env"])

    def test_configure_rejects_commands_and_yaml_injection(self):
        settings = [
            {"global_prep_cmd": "echo injected"}, {"prep_cmd": "echo injected"},
            {"apps": "/tmp/untrusted.json"}, {"encoder": "nvenc\nglobal_prep_cmd: injected"},
            {"encoder": {"nested": "bad"}}, {"hevc_mode": True}, {"upnp": "enabled; echo injected"},
        ]
        with patch.object(tools, "_host_run") as run:
            for value in settings:
                with self.subTest(value=value):
                    result = json.loads(tools.game_stream_handler({"action": "configure", "settings": value}))
                    self.assertFalse(result["success"])
            run.assert_not_called()

    def test_configure_preserves_existing_and_writes_allowed_settings(self):
        with patch.object(tools, "_host_run", side_effect=[(0, "custom: retained\nencoder: software\n"), (0, "WROTE")]) as run:
            result = json.loads(tools.game_stream_handler(
                {"action": "configure", "settings": {"encoder": "nvenc", "hevc_mode": 2}}))
        self.assertTrue(result["success"])
        command = run.call_args.args[2]
        encoded = shlex.split(command)[shlex.split(command).index("%s")+1]
        body = base64.b64decode(encoded).decode()
        self.assertIn("custom: retained\n", body)
        self.assertIn("encoder: nvenc\n", body)
        self.assertIn("hevc_mode: 2\n", body)

    @unittest.skipUnless(os.name == "posix", "executes remote POSIX command locally in temporary HOME")
    def test_unpair_name_is_data_in_real_python_command(self):
        name = "O'Brien\\'; print('INJECTED'); #"
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / ".config/sunshine/sunshine_state.json"
            state.parent.mkdir(parents=True)
            state.write_text(json.dumps({"clients": {"client-id": {"name": name}}}))
            def run(args, remote, local, timeout=30):
                p = subprocess.run(local, shell=True, capture_output=True, text=True,
                                   env={**os.environ, "HOME": tmp}, timeout=5)
                return p.returncode, p.stdout + p.stderr
            with patch.object(tools, "_host_run", side_effect=run):
                result = json.loads(tools.game_stream_handler({"action": "unpair", "client_name": name}))
            self.assertEqual(result["probe"], "FOUND:client-id")
            self.assertEqual(json.loads(state.read_text())["clients"]["client-id"]["name"], name)

    def test_install_role_visible_in_schema_and_selects_client(self):
        self.assertEqual(schemas.GAME_STREAM_SCHEMA["parameters"]["properties"]["role"]["enum"], ["host", "client"])
        with patch.object(tools, "_run", side_effect=[(0, "ID=apt"), (0, "installed")]) as run:
            result = json.loads(tools.game_stream_handler({"action": "install", "role": "client"}))
        self.assertEqual(result["package"], "moonlight-qt")
        self.assertIn("moonlight-qt", run.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
