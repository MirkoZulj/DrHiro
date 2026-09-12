# drHiro MCP server — raw JSON-RPC over HTTP (the TrueForge agent's tool surface).
# This is the production MCP served on :3100; the FastMCP scaffold (server.py)
# serves the OpenClaw gateway via /tools/* instead.
FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir mcp[cli] httpx uvicorn starlette redis PyJWT
COPY src/drhiro_mcp/sse_server.py /app/sse_server.py
EXPOSE 3100
CMD ["python", "/app/sse_server.py"]
