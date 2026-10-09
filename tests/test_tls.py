import asyncio
import threading
import unittest
from unittest.mock import patch

import httpx
from discord_lucas_mcp import server


class TLSResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_tls_initialization_does_not_stop_the_event_loop(self):
        entered, release = threading.Event(), threading.Event()
        context = object()
        loop_thread = threading.get_ident()
        loader_threads = []
        real_client = httpx.AsyncClient
        client = real_client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})))

        def create_context():
            loader_threads.append(threading.get_ident())
            entered.set()
            if not release.wait(2):
                raise RuntimeError("TLS初期化が受信スレッドを止めた")
            return context

        def factory(**kwargs):
            if kwargs.get("verify") is not context:
                create_context()
            return client

        with patch.object(server, "tls_context", side_effect=create_context, create=True), patch.object(server, "token", return_value="mock"), patch.object(server.httpx, "AsyncClient", side_effect=factory):
            task = asyncio.create_task(server.discord_read("/users/@me"))
            fallback = threading.Timer(0.5, release.set)
            fallback.start()
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                await asyncio.wait_for(asyncio.sleep(0.01), 0.2)
                self.assertNotEqual(loader_threads, [loop_thread])
            finally:
                release.set()
                await task
                fallback.cancel()

    async def test_tls_context_is_reused_and_certificate_validation_remains_enabled(self):
        with patch.object(server, "_tls_context", None, create=True), patch.object(server.ssl, "create_default_context", wraps=server.ssl.create_default_context) as loader:
            first = await asyncio.to_thread(server.tls_context)
            second = await asyncio.to_thread(server.tls_context)
            self.assertIs(first, second)
            self.assertEqual(loader.call_count, 1)
            self.assertTrue(first.check_hostname)
            self.assertEqual(first.verify_mode, server.ssl.CERT_REQUIRED)

    async def test_request_and_scope_metadata_both_use_the_shared_context(self):
        real_client = httpx.AsyncClient
        context = object()
        clients = [real_client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"id":"1"}))) for _ in range(2)]
        with patch.object(server, "tls_context", return_value=context), patch.object(server, "token", return_value="mock"), patch.object(server.httpx, "AsyncClient", side_effect=clients) as factory:
            await server.discord_read("/users/@me")
            await server.scope_metadata("/channels/1")
        self.assertEqual(factory.call_count, 2)
        self.assertTrue(all(call.kwargs["verify"] is context for call in factory.call_args_list))

    def test_explicit_ca_file_and_directory_keep_the_original_precedence(self):
        for environment, expected in (({"SSL_CERT_FILE": "certificate.pem", "SSL_CERT_DIR": "certificates"}, {"cafile": "certificate.pem"}),
                                      ({"SSL_CERT_DIR": "certificates"}, {"capath": "certificates"})):
            with patch.dict(server.os.environ, environment, clear=True), patch.object(server, "_tls_context", None), patch.object(server.ssl, "create_default_context", return_value=object()) as loader:
                server.tls_context()
                loader.assert_called_once_with(**expected)


if __name__ == "__main__":
    unittest.main()
