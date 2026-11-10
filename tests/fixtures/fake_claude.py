#!/usr/bin/env python3
"""Stand-in for `claude -p ... --output-format json` in runner tests.

Reads the MCP config the runner wrote, acts on the bus with the agent's run
token (takes a lock, which the runner's end-of-run must release), writes a
file, and prints a Claude-style JSON result. FAKE_AGENT_MODE=fail exits 1.
"""
import json
import os
import sys
import urllib.request
from pathlib import Path

args = sys.argv[1:]
prompt = args[args.index("-p") + 1]
config = json.loads(Path(args[args.index("--mcp-config") + 1]).read_text())
env = config["mcpServers"]["iac-bus"]["env"]
mode = os.environ.get("FAKE_AGENT_MODE", "ok")

import time as _time
_time.sleep(float(os.environ.get("FAKE_AGENT_SLEEP", "0")))
if mode == "fail":
    print("simulated crash", file=sys.stderr)
    sys.exit(1)

leaked = "BUS_API_TOKEN" in os.environ or "BUS_API_TOKEN" in env
req = urllib.request.Request(env["BUS_URL"] + "/bus/locks/acquire", method="POST",
                             data=json.dumps({"resource": "src/x.py", "agent": "spoof"}).encode(),
                             headers={"Authorization": "Bearer " + env["BUS_RUN_TOKEN"], "Content-Type": "application/json"})
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
lock = json.load(opener.open(req, timeout=10))
Path("src").mkdir(exist_ok=True)
Path("src/x.py").write_text("print('hi')\n")
summary = {"wrote": "src/x.py", "lock_holder": lock["agent"], "work_item": env["BUS_WORK_MESSAGE_ID"],
           "shared_token_leaked": leaked, "prompt_has_rules": "do not call claim_work" in prompt}
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": json.dumps(summary),
                  "total_cost_usd": 0}))
