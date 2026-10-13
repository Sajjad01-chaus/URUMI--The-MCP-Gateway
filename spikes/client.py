import asyncio
from mcp import Client

SERVER_URL = "http://127.0.0.1:8001/mcp"

async def main():
    async with Client(SERVER_URL) as client:
        tools = await client.list_tools()
        for tool in tools.tools:
            print(f"tool: {tool.name} - {tool.description}")

        result = await client.call_tool("add", {"a": 10, "b": 20})
        print("add(10, 20) ->", result.structured_content, "| error:", result.is_error)

        bad = await client.call_tool("add", {"a": "ten", "b": 20})
        print("add('ten', 20) -> error:", bad.is_error, "|", bad.content[0].text[:60])

if __name__ == "__main__":
    asyncio.run(main())