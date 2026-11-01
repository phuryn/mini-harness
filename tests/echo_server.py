"""A one-tool MCP server for the integration test."""
from mcp.server.mcpserver import MCPServer

app = MCPServer("echo")


@app.tool()
def shout(text: str) -> str:
    """Return the text in upper case."""
    return text.upper()


if __name__ == "__main__":
    app.run()
