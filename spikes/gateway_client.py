import asyncio
import sys
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

GATEWAY_URL = "http://127.0.0.1:8002/mcp"


async def main(api_key: str):
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {api_key}"}) as http:
        async with Client(streamable_http_client(GATEWAY_URL, http_client=http)) as gateway:
            tools = await gateway.list_tools()
            for tool in tools.tools:
                print(f"tool: {tool.name} - {tool.description}")

            result = await gateway.call_tool("demo__add", {"a": 10, "b": 20})
            print("demo__add(10, 20) ->", result.structured_content, "| error:", result.is_error)

            unknown = await gateway.call_tool("add", {"a": 1, "b": 2})
            print("add (no prefix) -> error:", unknown.is_error, "|", unknown.content[0].text)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "spike-secret-key"))