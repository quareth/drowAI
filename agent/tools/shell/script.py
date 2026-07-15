from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

from ..base_tool import BaseTool
from runtime_shared.workspace_files import RuntimeWorkspaceFile
from ..schemas import ToolResult
from .contracts import ShellScriptArgs
from .policy import CommandPolicy
from ._helpers import (
    STDOUT_SUMMARY_LIMIT,
    STDERR_SUMMARY_LIMIT,
    _build_script_command,
    build_shell_script_relative_path,
    _prepare_env,
    _resolve_cwd,
    _run_subprocess,
    render_shell_script_body,
    _strip_kali_welcome,
    build_tool_result,
    extract_error_lines,
    smart_truncate,
    workspace_root,
)


class ShellScriptTool(BaseTool):
    """
    Execute a multi-line script with workspace safeguards.
    
    The transport parameter controls execution routing (handled by executor):
    - "file-comm": Execute via Kali container queue
    - "pty": Execute in PTY session (script visible to users)
    - None: Executor auto-selects based on availability
    
    PTY support:
        - build_command() creates script file and interpreter command
        - Executor routes to PTY automatically when ENABLE_PTY_EXECUTION=true
        - transport parameter is optional; executor chooses the best path
    
    PTY execution improves visibility for debugging: scripts are persisted
    under workspace/scripts/ and streamed through the terminal session.
    """

    args_model = ShellScriptArgs

    def __init__(self) -> None:
        super().__init__()
        self._last_script_path: Optional[str] = None
        self._last_script_relative_path: Optional[str] = None

    def build_command(self, args: ShellScriptArgs) -> List[str]:
        """Return the interpreter command list for the prepared script."""
        relative_path = self._script_relative_path(args)
        write_script = args.transport not in {"file-comm", "pty"}
        command, script_path = _build_script_command(
            args,
            write_script=write_script,
            relative_path=relative_path,
        )
        self._last_script_path = str(script_path)
        return list(command)

    def _script_relative_path(self, args: ShellScriptArgs) -> str:
        path = self._last_script_relative_path
        if not path:
            path = build_shell_script_relative_path(args)
            self._last_script_relative_path = path
        return path

    def prepare_workspace_files(self, args: ShellScriptArgs) -> List[RuntimeWorkspaceFile]:
        return [
            RuntimeWorkspaceFile.from_text(
                relative_path=self._script_relative_path(args),
                content=render_shell_script_body(args),
                description="shell.script runtime script",
            )
        ]

    def parse_output(self, stdout: str, stderr: str, exit_code: int, args: ShellScriptArgs) -> Dict[str, Any]:
        """Parse script output into structured metadata."""
        clean_stdout = _strip_kali_welcome(stdout) if stdout else ""
        script_path = self._last_script_path
        try:
            if script_path:
                script_rel = os.path.relpath(script_path, workspace_root())
            else:
                script_rel = None
        except Exception:
            script_rel = script_path

        metadata: Dict[str, Any] = {
            "interpreter": args.interpreter,
            "script_path": script_rel,
            "exit_code": exit_code,
            "success": exit_code == 0,
            "strict_mode": bool(args.strict_mode),
            "output_length": len(clean_stdout),
            "has_errors": bool(stderr),
            "transport": args.transport or "direct",
        }

        if stderr:
            metadata["error_lines"] = extract_error_lines(stderr, max_matches=5)

        return {"shell_script": metadata}

    def create_artifacts(
        self,
        stdout: str,
        args: ShellScriptArgs,
        timestamp: Optional[int] = None,
        stderr: str = "",
    ) -> List[str]:
        """Persist script file and large outputs to artifacts/."""
        created: List[str] = []
        ts = timestamp or int(time.time())

        try:
            os.makedirs("artifacts", exist_ok=True)

            if self._last_script_path:
                created.append(self._last_script_path)

            if stdout and len(stdout) > 10 * 1024:
                out_path = os.path.join("artifacts", f"script_{ts}_output.txt")
                with open(out_path, "w", encoding="utf-8", errors="ignore") as f:
                    f.write(stdout)
                created.append(out_path)

            if stderr:
                err_path = os.path.join("artifacts", f"script_{ts}_errors.txt")
                with open(err_path, "w", encoding="utf-8", errors="ignore") as f:
                    f.write(stderr)
                created.append(err_path)
        except Exception:
            pass

        return created

    def run(self, args: ShellScriptArgs) -> ToolResult:
        overall_start = time.time()
        policy = CommandPolicy()

        # Validate script line by line
        for line in args.script.strip().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            policy_result = policy.validate(line)
            if not policy_result.allowed and policy_result.severity == "error":
                metadata = {
                    "shell_script": {
                        "interpreter": args.interpreter,
                        "exit_code": -1,
                        "success": False,
                        "strict_mode": bool(args.strict_mode),
                        "transport": args.transport or "direct",
                    },
                    "policy_violation": {
                        "matched_pattern": policy_result.matched_pattern,
                        "severity": policy_result.severity,
                        "line": line[:100],
                    },
                }
                tool_result = build_tool_result(
                    success=False,
                    start=overall_start,
                    stdout="",
                    stderr=f"Policy violation in script: {policy_result.reason} (line: {line[:50]})",
                    metadata=metadata,
                    exit_code=-1,
                )
                return tool_result

        cwd = _resolve_cwd(args.cwd)
        env = _prepare_env(args.env)
        command = self.build_command(args)
        exit_code, stdout, stderr, _duration = _run_subprocess(tuple(command), cwd, env, args.timeout_sec)

        clean_stdout = _strip_kali_welcome(stdout) if stdout else ""
        stdout_summary = smart_truncate(clean_stdout, total_limit=STDOUT_SUMMARY_LIMIT) if clean_stdout else ""
        stderr_summary = smart_truncate(stderr, total_limit=STDERR_SUMMARY_LIMIT) if stderr else ""

        metadata = self.parse_output(stdout, stderr, exit_code, args)
        artifacts = self.create_artifacts(stdout=stdout, args=args, stderr=stderr)

        tool_result = build_tool_result(
            success=exit_code == 0,
            start=overall_start,
            stdout=stdout_summary,
            stderr=stderr_summary,
            metadata=metadata,
            exit_code=exit_code,
        )
        try:
            tool_result.artifacts = artifacts
        except Exception:
            pass
        return tool_result
