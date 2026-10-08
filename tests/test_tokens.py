import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from discord_lucas_mcp import server


class TokenTests(unittest.TestCase):
    def test_explicit_token_file_has_same_priority_for_gateway_and_mcp(self):
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / "discord.env"
            file.write_text("DISCORD_BOT_TOKEN=file-value\n", encoding="utf-8")
            with patch.object(server, "TOKEN_FILE", file), patch.dict(os.environ, {"DISCORD_TOKEN_FILE": str(file), "DISCORD_BOT_TOKEN": "environment-value"}):
                self.assertEqual(server.token(), "file-value")

    def test_standalone_mcp_can_use_token_environment_without_file(self):
        with patch.dict(os.environ, {"DISCORD_TOKEN_FILE": "", "DISCORD_BOT_TOKEN": "environment-value"}):
            self.assertEqual(server.token(), "environment-value")


if __name__ == "__main__":
    unittest.main()
