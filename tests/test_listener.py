import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import threading
import tomllib
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord
import httpx

from discord_lucas_mcp.listener import (
    ERROR_REPLY, MentionListener, Settings, Store, build_codex_args,
    parse_final_response, qualifies, run_codex, subprocess_environment, utf16_chunks,
    runtime_singleton,
    main,
)


BASE = Path(__file__).absolute().parent
SETTINGS = Settings(101, 202, BASE / "runtime", BASE / "runtime" / "attachments", BASE / "codex.exe",
                    BASE / "python.exe", "existing-model")


def message(identifier=1, channel=3, guild=101, bot=False, webhook=None, mentioned=True, content="<@202> 質問"):
    return SimpleNamespace(
        id=identifier, channel=SimpleNamespace(id=channel), guild=SimpleNamespace(id=guild) if guild else None,
        author=SimpleNamespace(id=55, bot=bot, display_name="利用者"), webhook_id=webhook,
        mentions=[SimpleNamespace(id=202)] if mentioned else [], content=content, attachments=[],
    )


class MentionTests(unittest.TestCase):
    def test_config_load_validates_package_schema(self):
        valid = {
            "target_guild_id": "101", "bot_id": "202", "runtime_dir": str(SETTINGS.runtime_dir),
            "attachment_root": str(SETTINGS.attachment_root), "codex_path": str(SETTINGS.codex_path),
            "python_path": str(SETTINGS.python_path), "model": "existing-model", "dm_allowed_user_ids": ["55"],
        }
        with tempfile.TemporaryDirectory(dir=BASE) as folder:
            config = Path(folder) / "listener.json"
            config.write_text(json.dumps(valid), encoding="utf-8")
            loaded = Settings.load(config)
            self.assertEqual(loaded.dm_allowed_user_ids, (55,))
            self.assertEqual(loaded.target_guild_id, 101)
            self.assertFalse(hasattr(loaded, "mcp_server_path"))
            missing = dict(valid)
            missing.pop("python_path")
            for invalid in ([], missing, {**valid, "target_guild_id": True}, {**valid, "model": None},
                            {**valid, "runtime_dir": "relative"}, {**valid, "dm_allowed_user_ids": "55"},
                            {**valid, "timeout_seconds": 1.5}):
                config.write_text(json.dumps(invalid), encoding="utf-8")
                with self.assertRaises(ValueError):
                    Settings.load(config)

    def test_main_config_argument_environment_and_cwd_default(self):
        class Probe(Exception):
            pass

        for environment, arguments, expected in (({}, [], Path("listener.json")),
                                                  ({"DISCORD_LUCAS_CONFIG": "environment.json"}, [], Path("environment.json")),
                                                  ({"DISCORD_LUCAS_CONFIG": "environment.json"}, ["--config", "explicit.json"], Path("explicit.json"))):
            with patch.dict(os.environ, environment, clear=True), \
                 patch.object(Settings, "load", side_effect=Probe) as load:
                with self.assertRaises(Probe):
                    main(arguments)
                load.assert_called_once_with(expected)

    def test_only_real_human_mentions_in_target_guild(self):
        self.assertTrue(qualifies(message(), SETTINGS))
        for incoming in (message(guild=999), message(guild=None), message(bot=True), message(webhook=7),
                         message(mentioned=False, content="@ルーカス"), message(mentioned=False, content="<@202>")):
            self.assertFalse(qualifies(incoming, SETTINGS))
        reply = message(mentioned=False)
        reply.reference = SimpleNamespace(message_id=9, resolved=SimpleNamespace(author=SimpleNamespace(id=202)))
        self.assertTrue(qualifies(reply, SETTINGS))
        reply.reference.resolved.author.id = 303
        self.assertFalse(qualifies(reply, SETTINGS))

    def test_last_completed_agent_message_only(self):
        events = [
            {"type": "item.completed", "item": {"type": "reasoning", "text": "内部思考"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "途中"}},
            {"type": "item.updated", "item": {"type": "agent_message", "text": "未完"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": " 最終 "}},
            {"type": "turn.completed", "usage": {}},
        ]
        stdout = "通知\n" + "\n".join(json.dumps(item) for item in events)
        self.assertEqual(parse_final_response(stdout), "最終")
        for event in ({"type": "turn.failed"}, {"type": "error"}):
            with self.assertRaises(RuntimeError):
                parse_final_response(stdout + "\n" + json.dumps(event))
        with self.assertRaises(RuntimeError):
            parse_final_response(json.dumps(events[-2]))

    def test_chunks_preserve_every_character_and_utf16_limit(self):
        text = "あ" * 1999 + "😀" * 1001 + "終わり"
        chunks = utf16_chunks(text)
        self.assertEqual("".join(chunks), text)
        self.assertEqual(chunks[0], "あ" * 1999)
        self.assertTrue(all(0 < len(chunk.encode("utf-16-le")) // 2 <= 2000 for chunk in chunks))
        self.assertEqual(utf16_chunks(""), [])

    def test_cli_isolated_from_personal_config_and_tools(self):
        with patch.dict(os.environ, {"DISCORD_TOKEN_FILE": ""}):
            args = build_codex_args(SETTINGS)
        self.assertIn("--ignore-user-config", args)
        self.assertIn("--ignore-rules", args)
        self.assertIn("--ephemeral", args)
        self.assertEqual(args[args.index("--sandbox") + 1], "read-only")
        self.assertEqual(args[args.index("--model") + 1], SETTINGS.model)
        self.assertEqual(args[args.index("--cd") + 1], str(SETTINGS.attachment_root))
        overrides = {}
        for index, arg in enumerate(args):
            if arg == "-c":
                key, value = args[index + 1].split("=", 1)
                overrides[key] = tomllib.loads("value=" + value)["value"]
        for key in (
            "shell_tool", "multi_agent", "plugins", "apps", "goals", "view_image", "image_generation",
            "browser_use", "browser_use_external", "browser_use_full_cdp_access", "browser_annotation_api",
            "computer_use", "in_app_browser", "skill_search", "skill_mcp_dependency_install",
            "hooks", "memories", "tool_suggest",
        ):
            self.assertIs(overrides["features." + key], False)
        self.assertTrue(overrides["features.skip_host_skill_discovery"])
        self.assertNotIn("features.code_mode_host", overrides)
        self.assertNotIn("features.remote_plugin", overrides)
        self.assertEqual(overrides["project_doc_max_bytes"], 0)
        self.assertEqual(overrides["web_search"], "disabled")
        self.assertEqual(overrides["approval_policy"], "never")
        self.assertTrue(overrides["mcp_servers.discord-lucas.required"])
        self.assertEqual(overrides["mcp_servers.discord-lucas.enabled_tools"], ["discord_read", "discord_write", "discord_upload"])
        self.assertEqual(overrides["mcp_servers.discord-lucas.tools.discord_write.approval_mode"], "approve")
        self.assertEqual(overrides["mcp_servers.discord-lucas.tools.discord_upload.approval_mode"], "approve")
        self.assertEqual(overrides["mcp_servers.discord-lucas.env"], {
            "DISCORD_ALLOWED_GUILD_ID": "101", "DISCORD_ALLOWED_FILE_ROOT": str(SETTINGS.attachment_root),
        })
        self.assertEqual(overrides["mcp_servers.discord-lucas.args"], ["-m", "discord_lucas_mcp"])
        self.assertFalse(any(key.startswith("mcp_servers.") and not key.startswith("mcp_servers.discord-lucas.") for key in overrides))
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", args)
        self.assertNotIn("--add-dir", args)

    def test_token_file_path_propagates_but_credentials_do_not(self):
        token_file = str(BASE / "external-token.env")
        with patch.dict(os.environ, {"DISCORD_TOKEN_FILE": token_file, "DISCORD_BOT_TOKEN": "bot-secret",
                                     "OPENAI_API_KEY": "api-secret"}):
            args = build_codex_args(SETTINGS, "88")
            env_value = next(value.split("=", 1)[1] for value in args if value.startswith("mcp_servers.discord-lucas.env="))
            mcp_env = tomllib.loads("value=" + env_value)["value"]
            self.assertEqual(mcp_env["DISCORD_TOKEN_FILE"], token_file)
            self.assertEqual(mcp_env["DISCORD_ALLOWED_DM_CHANNEL_ID"], "88")
            child_env = subprocess_environment()
            self.assertEqual(child_env["DISCORD_TOKEN_FILE"], token_file)
            for secret in ("DISCORD_BOT_TOKEN", "OPENAI_API_KEY"):
                self.assertNotIn(secret, child_env)
                self.assertNotIn(secret, mcp_env)
            self.assertNotIn("bot-secret", " ".join(args))
            self.assertNotIn("api-secret", " ".join(args))

    def test_gateway_requires_external_token_file(self):
        with patch.dict(os.environ, {"DISCORD_TOKEN_FILE": "", "DISCORD_BOT_TOKEN": "bot-secret"}), \
             patch.object(Settings, "load", return_value=SETTINGS):
            with self.assertRaisesRegex(ValueError, "DISCORD_TOKEN_FILE"):
                main([])

    def test_subprocess_uses_stdin_no_shell_and_does_not_pass_secret_env(self):
        stdout = '\n'.join(json.dumps(event) for event in (
            {"type": "item.completed", "item": {"type": "agent_message", "text": "回答"}},
            {"type": "turn.completed"},
        ))
        with tempfile.TemporaryDirectory(dir=BASE) as folder, \
             patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "private", "OPENAI_API_KEY": "private"}), \
             patch("discord_lucas_mcp.listener.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=stdout, stderr="")) as run:
            settings = replace(SETTINGS, runtime_dir=Path(folder))
            self.assertEqual(run_codex(settings, "質問"), "回答")
            kwargs = run.call_args.kwargs
            self.assertEqual(kwargs["input"], "質問")
            self.assertFalse(kwargs["shell"])
            self.assertEqual(kwargs["timeout"], 180)
            self.assertNotIn("DISCORD_BOT_TOKEN", kwargs["env"])
            self.assertNotIn("OPENAI_API_KEY", subprocess_environment())

    def test_dm_child_scope_is_only_incoming_dm_channel(self):
        with tempfile.TemporaryDirectory(dir=BASE) as folder, \
             patch("discord_lucas_mcp.listener.subprocess.run", side_effect=subprocess.TimeoutExpired("codex", 1)) as run:
            settings = replace(SETTINGS, runtime_dir=Path(folder))
            with self.assertRaises(subprocess.TimeoutExpired):
                run_codex(settings, json.dumps({"incoming": {"message_id": "77", "channel_id": "88", "guild_id": None}}))
            args = run.call_args.args[0]
            env = next(value.split("=", 1)[1] for value in args if value.startswith("mcp_servers.discord-lucas.env="))
            self.assertEqual(tomllib.loads("value=" + env)["value"]["DISCORD_ALLOWED_DM_CHANNEL_ID"], "88")
            self.assertNotIn("DISCORD_ALLOWED_DM_CHANNEL_ID", " ".join(build_codex_args(settings)))

    def test_timeout_diagnostic_preserves_output_and_redacts_secrets(self):
        with tempfile.TemporaryDirectory(dir=BASE) as folder, \
             patch.dict(os.environ, {"TEST_SECRET": "sensitive-value"}), \
             patch("discord_lucas_mcp.listener.subprocess.run", side_effect=subprocess.TimeoutExpired(
                 "codex", 1, output=b"progress sensitive-value", stderr=b"sk-secret1234567890")):
            settings = replace(SETTINGS, runtime_dir=Path(folder))
            with self.assertRaises(subprocess.TimeoutExpired):
                run_codex(settings, json.dumps({"incoming": {"message_id": "77", "guild_id": "101"}}))
            diagnostic = json.loads((Path(folder) / "diagnostics" / "77.json").read_text(encoding="utf-8"))
            self.assertEqual(diagnostic["state"], "timeout")
            self.assertIn("progress", diagnostic["stdout"])
            self.assertIn("[非表示]", diagnostic["stdout"])
            self.assertNotIn("sensitive-value", json.dumps(diagnostic))
            self.assertNotIn("sk-secret1234567890", json.dumps(diagnostic))
            self.assertIn("started_at", diagnostic)
            self.assertIn("finished_at", diagnostic)

    def test_dedup_survives_restart_and_history_stays_in_channel(self):
        with tempfile.TemporaryDirectory(dir=BASE) as folder:
            path = Path(folder) / "state.sqlite3"
            store = Store(path, 2)
            self.assertTrue(store.claim("1", "3"))
            store.remember("3", {"message_id": "1"}, "一回目")
            store.remember("3", {"message_id": "2"}, "二回目")
            store.remember("3", {"message_id": "3"}, "三回目")
            store.connection.close()
            restarted = Store(path, 2)
            try:
                self.assertFalse(restarted.claim("1", "3"))
                self.assertEqual([turn["response"] for turn in restarted.history("3")], ["二回目", "三回目"])
                self.assertEqual(restarted.history("4"), [])
            finally:
                restarted.connection.close()

    @unittest.skipUnless(os.name == "nt", "Windows named mutexの検証")
    def test_singleton_rejects_other_process_and_releases_after_exit(self):
        code = """from pathlib import Path
import sys
from discord_lucas_mcp.listener import runtime_singleton
try:
    with runtime_singleton(Path(sys.argv[1])):
        print('acquired')
except RuntimeError:
    print('occupied')
"""
        with tempfile.TemporaryDirectory(dir=BASE) as folder:
            command = [sys.executable, "-X", "utf8", "-c", code, folder]
            with runtime_singleton(Path(folder)):
                child = subprocess.run(command, cwd=BASE, capture_output=True, text=True, encoding="utf-8", timeout=20, shell=False)
                self.assertEqual(child.returncode, 0)
                self.assertEqual(child.stdout.strip(), "occupied")
                with self.assertRaisesRegex(RuntimeError, "既に起動"):
                    with runtime_singleton(Path(folder) / "."):
                        self.fail("同一runtimeの二重起動")
            child = subprocess.run(command, cwd=BASE, capture_output=True, text=True, encoding="utf-8", timeout=20, shell=False)
            self.assertEqual(child.returncode, 0)
            self.assertEqual(child.stdout.strip(), "acquired")


class DispatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=BASE)
        root = Path(self.temporary.name)
        self.settings = replace(SETTINGS, runtime_dir=root, attachment_root=root)
        self.store = Store(root / "state.sqlite3", 10)
        self.posts = []

        async def request(method, path, body=None, **kwargs):
            self.posts.append((method, path, body))
            return {"status": 200, "data": {"id": "888"}}

        self.request = request
        self.listener = MentionListener(self.settings, self.store, request)

    async def asyncTearDown(self):
        await self.listener.close()
        self.store.connection.close()
        self.temporary.cleanup()

    async def test_channel_serial_order_dedup_and_parallel_other_channel(self):
        first_started, release_first, other_finished = threading.Event(), threading.Event(), threading.Event()
        received = []

        def runner(settings, prompt):
            payload = json.loads(prompt)
            incoming = payload["incoming"]
            received.append(payload)
            if incoming["message_id"] == "1":
                first_started.set()
                if not release_first.wait(3):
                    raise RuntimeError("テスト同期失敗")
            if incoming["channel_id"] == "4":
                other_finished.set()
            return "回答" + incoming["message_id"]

        self.listener.runner = runner
        await self.listener.on_message(message(1))
        self.assertTrue(await asyncio.to_thread(first_started.wait, 2))
        await self.listener.on_message(message(2))
        await self.listener.on_message(message(1))
        await self.listener.on_message(message(3, channel=4))
        try:
            self.assertTrue(await asyncio.to_thread(other_finished.wait, 2))
            self.assertEqual([item["incoming"]["message_id"] for item in received if item["incoming"]["channel_id"] == "3"], ["1"])
        finally:
            release_first.set()
        await asyncio.gather(*(queue.join() for queue in self.listener.queues.values()))
        main_channel = [item for item in received if item["incoming"]["channel_id"] == "3"]
        self.assertEqual([item["incoming"]["message_id"] for item in main_channel], ["1", "2"])
        self.assertEqual(main_channel[1]["history"][0]["response"], "回答1")
        self.assertEqual(next(item for item in received if item["incoming"]["channel_id"] == "4")["history"], [])
        self.assertEqual(len(self.posts), 3)
        for method, path, body in self.posts:
            self.assertEqual(method, "POST")
            self.assertEqual(body["allowed_mentions"], {"parse": []})
            self.assertIn(body["message_reference"]["message_id"], ("1", "2", "3"))

    async def test_failed_delivery_is_never_replayed_or_error_posted(self):
        attempts = []

        async def timeout(method, path, body):
            attempts.append(body)
            raise TimeoutError("秘密を含みうる詳細")

        self.listener.api_request = timeout
        self.listener.runner = lambda settings, prompt: "😀" * 3000
        await self.listener.on_message(message())
        await self.listener.queues[3].join()
        await self.listener.on_message(message())
        self.assertEqual(len(attempts), 1)
        self.assertEqual(self.store.connection.execute("SELECT state FROM messages WHERE message_id='1'").fetchone()[0], "delivery_unknown")
        self.assertEqual(self.store.history("3"), [])

    async def test_codex_error_replies_with_safe_message(self):
        def failure(settings, prompt):
            raise RuntimeError("秘密やCLI stderr")

        self.listener.runner = failure
        await self.listener.on_message(message())
        await self.listener.queues[3].join()
        self.assertEqual(self.posts[0][2]["content"], ERROR_REPLY)
        self.assertEqual(self.store.history("3"), [])
        self.assertEqual(self.store.reply("1")["failure"]["type"], "RuntimeError")

    def test_outbox_content_and_progress_survive_store_restart(self):
        self.store.claim("70", "3")
        incoming = {"message_id": "70", "channel_id": "3", "guild_id": None}
        self.store.enqueue_reply(incoming, "未送信返信", True, {"type": "TimeoutExpired", "diagnostic": "diagnostics/70.json"})
        self.store.delivery_state("70", "delivery_unknown", 0)
        self.store.connection.close()
        self.store = Store(Path(self.temporary.name) / "state.sqlite3", 10)
        self.listener.store = self.store
        recovered = self.store.reply("70")
        self.assertEqual(recovered["response"], "未送信返信")
        self.assertEqual(recovered["state"], "delivery_unknown")
        self.assertEqual(recovered["next_chunk"], 0)
        self.assertEqual(recovered["failure"]["type"], "TimeoutExpired")
        self.assertIsNone(recovered["incoming"]["guild_id"])

    async def test_gateway_intents_and_disconnect_status(self):
        self.assertTrue(self.listener.intents.guilds)
        self.assertTrue(self.listener.intents.guild_messages)
        self.assertFalse(self.listener.intents.message_content)
        self.assertFalse(self.listener.intents.members)
        self.assertTrue(self.listener.intents.dm_messages)
        await self.listener.on_disconnect()
        state = json.loads((self.settings.runtime_dir / "listener-status.json").read_text(encoding="utf-8"))
        self.assertFalse(state["connected"])
        self.assertEqual(state["pid"], os.getpid())
        self.assertEqual(state["target_guild_id"], "101")

    async def test_dm_allowlist_cached_members_fetch_and_outsider_rejection(self):
        dm = message(guild=None, mentioned=False)
        self.listener.settings = replace(self.settings, dm_allowed_user_ids=(55,))
        self.assertTrue(await self.listener.authorized(dm))
        self.listener.settings = self.settings
        self.assertFalse(await self.listener.authorized(dm))
        guild = SimpleNamespace(get_member=lambda identifier: SimpleNamespace(id=identifier), fetch_member=AsyncMock())
        self.listener._connection._guilds[101] = guild
        self.assertTrue(await self.listener.authorized(dm))
        guild.fetch_member.assert_not_awaited()
        guild.get_member = lambda identifier: None
        guild.fetch_member.return_value = SimpleNamespace(id=55, bot=False)
        self.assertTrue(await self.listener.authorized(dm))
        guild.fetch_member.assert_awaited_once_with(55)
        guild.fetch_member.side_effect = discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "unknown member")
        self.assertFalse(await self.listener.authorized(dm))
        self.assertFalse(await self.listener.authorized(message(guild=None, bot=True)))

    async def test_uncached_reply_fetch_is_target_bot_and_guild_only(self):
        incoming = message(mentioned=False)
        incoming.reference = SimpleNamespace(message_id=9, resolved=None)
        incoming.channel.fetch_message = AsyncMock(return_value=SimpleNamespace(author=SimpleNamespace(id=202)))
        self.assertTrue(await self.listener.authorized(incoming))
        incoming.channel.fetch_message.return_value.author.id = 999
        self.assertFalse(await self.listener.authorized(incoming))
        incoming.guild.id = 999
        incoming.channel.fetch_message.reset_mock()
        self.assertFalse(await self.listener.authorized(incoming))
        incoming.channel.fetch_message.assert_not_awaited()

    async def test_dm_request_scope_and_saved_outbox(self):
        captured = []

        async def request(method, path, body=None, **kwargs):
            captured.append((method, path, kwargs))
            return {"data": {"id": "500"}}

        self.listener.settings = replace(self.settings, dm_allowed_user_ids=(55,))
        self.listener.api_request = request
        self.listener.runner = lambda settings, prompt: "DM回答"
        await self.listener.on_message(message(guild=None, mentioned=False))
        await self.listener.queues[3].join()
        self.assertEqual(captured, [("POST", "/channels/3/messages", {"allowed_dm_channel_id": "3"})])
        self.assertIsNone(self.store.reply("1")["incoming"]["guild_id"])
        self.assertEqual(self.store.reply("1")["response"], "DM回答")

    async def test_reconnect_recovers_known_not_sent_without_rerunning_codex(self):
        runs, attempts = [], []

        def runner(settings, prompt):
            runs.append(prompt)
            return "回復する返信"

        async def disconnected(method, path, body=None, **kwargs):
            attempts.append(method)
            try:
                raise httpx.ConnectError("ネットワーク切断")
            except httpx.ConnectError:
                raise ValueError("送信に失敗") from None

        self.listener.runner, self.listener.api_request = runner, disconnected
        await self.listener.on_message(message())
        await self.listener.queues[3].join()
        self.assertEqual(self.store.reply("1")["state"], "not_sent")
        self.listener.api_request = self.request
        await self.listener.on_resumed()
        await self.listener.on_resumed()
        self.assertEqual(len(runs), 1)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.posts[0][2]["content"], "回復する返信")
        self.assertEqual(self.store.reply("1")["state"], "completed")

    async def test_unknown_delivery_checks_existing_reply_then_only_sends_remainder(self):
        posted, reads, runs = [], [], []

        def runner(settings, prompt):
            runs.append(prompt)
            return "😀" * 1500

        async def request(method, path, body=None, **kwargs):
            if method == "GET":
                reads.append(kwargs)
                return {"data": [{"id": "500", "author": {"id": "202"}, "message_reference": {"message_id": "1"}, "content": posted[0]["content"]}]}
            posted.append(body)
            if len(posted) == 1:
                raise httpx.ReadTimeout("受理後に応答が消失")
            return {"data": {"id": "501"}}

        self.listener.runner, self.listener.api_request = runner, request
        await self.listener.on_message(message())
        await self.listener.queues[3].join()
        self.assertEqual(self.store.reply("1")["state"], "delivery_unknown")
        await self.listener.on_resumed()
        self.assertEqual(len(runs), 1)
        self.assertEqual(len(reads), 1)
        self.assertEqual(len(posted), 2)
        self.assertEqual("".join(body["content"] for body in posted), "😀" * 1500)
        self.assertEqual(self.store.reply("1")["state"], "completed")

    async def test_failed_reconciliation_never_downgrades_to_safe_resend(self):
        self.store.claim("1", "3")
        self.store.enqueue_reply({"message_id": "1", "channel_id": "3", "guild_id": "101"}, "既存かもしれない", False)
        self.store.delivery_state("1", "delivery_unknown")
        methods = []

        async def request(method, path, **kwargs):
            methods.append(method)
            raise httpx.ConnectError("読取接続不可")

        self.listener.api_request = request
        await self.listener.recover_replies()
        await self.listener.recover_replies()
        self.assertEqual(methods, ["GET", "GET"])
        self.assertEqual(self.store.reply("1")["state"], "delivery_unknown")

    async def test_periodic_outbox_recovery_without_gateway_reconnect(self):
        runs, recovered = [], asyncio.Event()

        def runner(settings, prompt):
            runs.append(prompt)
            return "Gateway接続中の配送回復"

        async def request(method, path, body=None, **kwargs):
            raise httpx.ConnectError("HTTPだけ切断")

        self.listener.runner, self.listener.api_request = runner, request
        await self.listener.on_message(message())
        await self.listener.queues[3].join()
        self.assertEqual(self.store.reply("1")["state"], "not_sent")

        async def connected(method, path, body=None, **kwargs):
            result = await self.request(method, path, body, **kwargs)
            recovered.set()
            return result

        self.listener.api_request = connected
        self.listener.recovery_interval = 0.01
        with patch.object(self.listener, "is_ready", return_value=True):
            await self.listener.setup_hook()
            await asyncio.wait_for(recovered.wait(), 1)
            await asyncio.sleep(0.02)
        self.assertEqual(len(runs), 1)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.store.reply("1")["state"], "completed")

    async def test_permanent_http_rejection_is_not_automatically_reposted(self):
        attempts = []

        async def forbidden(method, path, body=None, **kwargs):
            attempts.append(method)
            raise ValueError('Discord HTTP 403: {"message": "Missing Permissions"}')

        self.listener.runner = lambda settings, prompt: "配送できない返信"
        self.listener.api_request = forbidden
        await self.listener.on_message(message())
        await self.listener.queues[3].join()
        await self.listener.recover_replies()
        await self.listener.on_resumed()
        self.assertEqual(attempts, ["POST"])
        self.assertEqual(self.store.reply("1")["state"], "rejected")

    async def test_ready_checks_actual_bot_and_guild_before_marking_connected(self):
        self.listener._connection.user = SimpleNamespace(id=202)
        self.listener._connection._guilds[101] = SimpleNamespace(id=101)
        await self.listener.on_ready()
        state = json.loads((self.settings.runtime_dir / "listener-status.json").read_text(encoding="utf-8"))
        self.assertTrue(state["connected"])
        self.assertEqual(state["bot_id"], "202")
        self.listener._connection.user = SimpleNamespace(id=999)
        await self.listener.on_ready()
        state = json.loads((self.settings.runtime_dir / "listener-status.json").read_text(encoding="utf-8"))
        self.assertFalse(state["connected"])
        self.assertTrue(self.listener.is_closed())


if __name__ == "__main__":
    unittest.main()
