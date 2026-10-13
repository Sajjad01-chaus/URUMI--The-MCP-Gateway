from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

mcp = MCPServer(name="demo-upstream", instructions="Tiny demo server for the gateway spike.")

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def add(a: int, b: int) -> int:
    """Add two integers and return the sum."""
    return a + b

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def echo(text: str) -> str:
    """Return the same text back."""
    return text

if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="127.0.0.1", port=8001, stateless_http=True, json_response=True)