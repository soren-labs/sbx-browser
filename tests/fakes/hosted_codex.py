#!/usr/bin/env python3
"""Mock hosted Codex executes local tests/commits and supplies review JSON."""

import json
import subprocess
import sys
from pathlib import Path


def emit(event):
    print(json.dumps(event), flush=True)


prompt = sys.argv[-1]
root = Path.cwd()
emit({"type": "thread.started", "thread_id": "mock-hosted-thread"})
if "SBX independent review." in prompt:
    fixed = (root / "hello.txt").exists() and "fixed" in (root / "hello.txt").read_text()
    output = {
        "verdict": "approve" if fixed else "request_changes",
        "findings": []
        if fixed
        else [{"message": "Replace the placeholder greeting with the fixed greeting."}],
    }
else:
    fixed = "fix" in prompt.lower()
    (root / "hello.txt").write_text("hello fixed\n" if fixed else "hello placeholder\n")
    test = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; assert Path('hello.txt').read_text().startswith('hello')",
        ],
        capture_output=True,
        text=True,
    )
    emit(
        {
            "type": "item.completed",
            "item": {
                "id": "test",
                "type": "command_execution",
                "command": "python greeting test",
                "aggregated_output": "1 passed",
                "exit_code": test.returncode,
                "status": "completed",
            },
        }
    )
    subprocess.run(["git", "add", "hello.txt"], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Mock Codex",
            "-c",
            "user.email=mock@example.test",
            "commit",
            "-m",
            "Fix greeting" if fixed else "Add greeting",
        ],
        check=True,
        capture_output=True,
    )
    emit(
        {
            "type": "item.completed",
            "item": {
                "id": "change",
                "type": "file_change",
                "changes": [{"path": "hello.txt", "kind": "update"}],
                "status": "completed",
            },
        }
    )
    output = "Greeting fixed; 1 passed." if fixed else "Greeting created; 1 passed."
emit(
    {
        "type": "item.completed",
        "item": {
            "id": "result",
            "type": "agent_message",
            "text": json.dumps(output) if isinstance(output, dict) else output,
        },
    }
)
emit({"type": "turn.completed", "usage": {"input_tokens": 20, "output_tokens": 10}})
