import sys
import tempfile
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class MCPPackageTests(unittest.IsolatedAsyncioTestCase):
    async def test_installed_package_serves_stdio_from_an_unrelated_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            parameters = StdioServerParameters(command=sys.executable, args=["-m", "discord_lucas_mcp"], cwd=folder)
            async with stdio_client(parameters) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    initialized = await session.initialize()
                    tools = await session.list_tools()
                    self.assertEqual(initialized.serverInfo.name, "discord-lucas")
                    self.assertEqual({tool.name for tool in tools.tools}, {"discord_read", "discord_write", "discord_upload"})


if __name__ == "__main__":
    unittest.main()
