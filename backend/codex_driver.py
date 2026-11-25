"""Bridge a single Splee task to an ephemeral Codex app-server thread on Linux."""
import ctypes
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import tomllib

import jsonschema


def send(stream, message):
    stream.write(json.dumps(message) + "\n")
    stream.flush()


def die_with_parent():
    ctypes.CDLL(None).prctl(1, signal.SIGKILL)
    if os.getppid() == 1:
        os._exit(1)


def execute(proc, task, workdir):
    def rpc(method, params, ident=None):
        msg = {"method": method, "params": params}
        if ident is not None:
            msg["id"] = ident
        send(proc.stdin, msg)

    def messages():
        for line in proc.stdout:
            yield json.loads(line)
        raise RuntimeError("Codex disconnected")

    def result_for(ident):
        for msg in messages():
            if msg.get("id") == ident and "method" not in msg:
                if "error" in msg:
                    raise RuntimeError(str(msg["error"])[:400])
                return msg["result"]

    rpc("initialize", {"clientInfo":{"name":"splee", "version":"1.0"},
                       "capabilities":{"experimentalApi":True}}, 1)
    result_for(1)
    rpc("initialized", {})
    rpc("account/read", {"refreshToken":False}, 2)
    account = result_for(2).get("account") or {}
    if account.get("type") != "chatgpt":
        raise RuntimeError("Splee requires the authorized ChatGPT subscription")

    tool_names = {"splee_" + tool["name"].removeprefix("mcp__"):tool["name"] for tool in task["tools"]}
    wire_tools = [dict(tool, name=wire) for wire,tool in zip(tool_names,task["tools"])]
    for wire, name in tool_names.items():
        task["prompt"] = task["prompt"].replace(name, wire)
        task["system"] = task["system"].replace(name, wire)
    rpc("thread/start", {
        "cwd":workdir, "ephemeral":True, "environments":[], "approvalPolicy":"never",
        "sandbox":"read-only", "model":task["model"], "dynamicTools":wire_tools,
        "developerInstructions":task["system"]+"\nReturn one JSON value matching the supplied schema.",
        "config":{"web_search":"live" if task["web"] else "disabled", "model_reasoning_effort":"low",
                  "project_doc_max_bytes":0, "features.multi_agent":False}
    }, 3)
    thread = result_for(3)["thread"]["id"]
    prompt = task["prompt"]+"\n\nRequired JSON schema:\n"+json.dumps(task["schema"])
    rpc("turn/start", {"threadId":thread, "environments":[],
        "input":[{"type":"text","text":prompt,"text_elements":[]}]}, 4)
    allowed = {tool["name"] for tool in task["tools"]}
    calls = 0
    final = ""
    for msg in messages():
        method = msg.get("method")
        p = msg.get("params", {})
        if "error" in msg and "id" in msg:
            raise RuntimeError(str(msg["error"])[:400])
        if method == "item/tool/call" and "id" in msg:
            name = tool_names.get(p.get("tool"))
            if name not in allowed or p.get("threadId") != thread:
                raise RuntimeError("Unexpected tool request")
            calls += 1
            if calls > task["max_turns"]:
                raise RuntimeError("Task tool limit reached")
            send(sys.stdout, {"tool":name,"args":p["arguments"]})
            reply = json.loads(sys.stdin.readline())
            send(proc.stdin, {"id":msg["id"],"result":{"success":not reply["error"],
                 "contentItems":[{"type":"inputText","text":reply["text"]}]}})
        elif "id" in msg and method:
            send(proc.stdin, {"id":msg["id"],"error":{"code":-32601,"message":"Unavailable"}})
        elif method == "item/started" and p["item"]["type"] == "webSearch":
            calls += 1
            if not task["web"] or calls > task["max_turns"]:
                raise RuntimeError("Web search limit reached")
        elif method == "item/completed" and p["item"]["type"] == "agentMessage":
            final = p["item"]["text"].strip()
        elif method == "turn/completed":
            if p["turn"]["status"] != "completed":
                raise RuntimeError("Codex turn "+p["turn"]["status"]+": "+str(p["turn"].get("error"))[:350])
            if final.startswith("```"):
                final = "\n".join(final.splitlines()[1:-1])
            value = json.loads(final)
            if not jsonschema.Draft7Validator(task["schema"]).is_valid(value):
                raise ValueError("Answer did not match Splee schema")
            send(sys.stdout, {"answer":value})
            return


def main():
    task = json.loads(sys.stdin.readline())
    config = Path.home()/".codex/config.toml"
    settings = tomllib.loads(config.read_text()) if config.exists() else {}
    args = [os.environ["SPLEE_CODEX_BIN"], "app-server", "--stdio"]
    for feature in ("shell_tool","unified_exec","hooks","plugins","apps","multi_agent","tool_suggest","remote_plugin"):
        args += ["--disable",feature]
    for name in settings.get("mcp_servers", {}):
        if not name.replace("_","").replace("-","").isalnum(): raise RuntimeError("Unsupported MCP config key")
        args += ["-c","mcp_servers."+name+".enabled=false"]
    with tempfile.TemporaryDirectory(prefix="splee-job-") as cwd:
        proc = subprocess.Popen(args,cwd=cwd,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL,text=True,preexec_fn=die_with_parent)
        try:
            execute(proc,task,cwd)
        finally:
            proc.kill()
            proc.wait()


if __name__ == "__main__":
    try:
        main()
    except (json.JSONDecodeError, ValueError):
        send(sys.stdout, {"invalid":True})
    except Exception as exc:
        send(sys.stdout, {"failed":str(exc)[:500]})
