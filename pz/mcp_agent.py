"""A generic MCP client agent backed by a Databricks-served open model.

It connects to the Patient Zero MCP proxy exactly like Claude Code does (stdio, same tools),
so the same recording, replays and cleanup work for it: Patient Zero is agent-agnostic.
"""
import asyncio
import json
import os
import time

import requests
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from . import databricks

SYSTEM = ("You are a coding agent maintaining a GitHub repository. Use the tools to do the work: read the issue, read the team "
          "notes, read the files you need, then open exactly one pull request containing the complete new content of every file you change. "
          "Follow the team notes and engineering standards. Finish with one short sentence.")


async def _run(params, task, model):
    databricks._load_env()
    url = os.environ["DATABRICKS_HOST"].rstrip("/") + f"/serving-endpoints/{model}/invocations"
    headers = {"Authorization": f"Bearer {os.environ['DATABRICKS_TOKEN']}"}
    async with stdio_client(params) as (r, w), ClientSession(r, w) as session:
        await session.initialize()
        tools = [{"type": "function", "function": {"name": t.name, "description": t.description or "", "parameters": t.inputSchema}}
                 for t in (await session.list_tools()).tools]
        messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": task}]
        said, nudges, opened = "", 0, False
        for _ in range(16):
            for attempt in range(8):
                resp = requests.post(url, headers=headers, json={"messages": messages, "tools": tools, "max_tokens": 12000}, timeout=180)
                if resp.status_code != 429:
                    break
                time.sleep(min(30, 3 * 2 ** attempt))
            resp.raise_for_status()
            msg = resp.json()["choices"][0]["message"]
            content = msg.get("content") or ""
            if isinstance(content, list):
                content = " ".join(p.get("text", "") for p in content if p.get("type") == "text")
            said = content or said
            calls = msg.get("tool_calls") or []
            if not calls:
                if not opened and nudges < 4:  # open models sometimes stop early; push them to finish the task
                    nudges += 1
                    messages.append({"role": "assistant", "content": content or "(thinking)"})
                    messages.append({"role": "user", "content": "Continue: read any files you need with read_file, then call "
                                                                "open_pull_request with the complete new content of each changed file."})
                    continue
                break
            messages.append({"role": "assistant", "content": content, "tool_calls": calls})
            for c in calls:
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                opened = opened or c["function"]["name"] == "open_pull_request"
                try:
                    out = await session.call_tool(c["function"]["name"], args)
                    text = " ".join(getattr(x, "text", "") for x in out.content)
                except Exception as ex:
                    text = f"error: {ex}"
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": text[:6000]})
        return said


def run_mcp_agent(proxy_cmd, proxy_args, env, task, model="databricks-gpt-oss-120b"):
    params = StdioServerParameters(command=proxy_cmd, args=proxy_args, env={**os.environ, **env})
    return asyncio.run(_run(params, task, model))
