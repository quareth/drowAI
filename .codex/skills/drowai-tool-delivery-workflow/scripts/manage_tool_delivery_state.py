"""Validate one-tool delivery state and reset only its explicit child ledgers."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Mapping, NamedTuple, Sequence

import yaml


STATUSES = {
    "READY",
    "ANALYZING",
    "DECISION_PENDING",
    "DECISION_RECORDED",
    "BRANCH_READY",
    "GUIDE_REVIEW",
    "IMPLEMENTING",
    "IMPLEMENTATION_REVIEW",
    "SECURITY_REVIEW",
    "MECHANICAL_VALIDATION",
    "QUALITY_REVIEW",
    "REFACTORING",
    "FINAL_REVIEW",
    "READY_FOR_PR",
    "PR_OPENED",
    "AWAITING_MERGE",
    "MERGED",
    "BLOCKED",
    "NEEDS_CLARIFICATION",
}
NORMAL_TRANSITIONS = {
    "READY": {"ANALYZING"},
    "ANALYZING": {"BRANCH_READY", "DECISION_PENDING"},
    "DECISION_PENDING": {"DECISION_RECORDED"},
    "DECISION_RECORDED": {"READY"},
    "BRANCH_READY": {"GUIDE_REVIEW"},
    "GUIDE_REVIEW": {"IMPLEMENTING"},
    "IMPLEMENTING": {"IMPLEMENTATION_REVIEW"},
    "IMPLEMENTATION_REVIEW": {"IMPLEMENTING", "SECURITY_REVIEW"},
    "SECURITY_REVIEW": {"IMPLEMENTING", "MECHANICAL_VALIDATION"},
    "MECHANICAL_VALIDATION": {"IMPLEMENTING", "QUALITY_REVIEW"},
    "QUALITY_REVIEW": {"REFACTORING", "FINAL_REVIEW"},
    "REFACTORING": {"SECURITY_REVIEW"},
    "FINAL_REVIEW": {"IMPLEMENTING", "READY_FOR_PR"},
    "READY_FOR_PR": {"PR_OPENED"},
    "PR_OPENED": {"AWAITING_MERGE"},
    "AWAITING_MERGE": {"MERGED"},
    "MERGED": {"READY"},
}
REQUIRED_PR_GATES = {
    "capability",
    "guide_review",
    "phase_reviews",
    "final_implementation",
    "static_security",
    "mechanical_validation",
    "quality_review",
    "final_tests",
}
DELIVERY_LIVE_STATE = ".codex/agents/drowai-tool-delivery-workflow-state.md"


class StatePair(NamedTuple):
    """Map one committed reset example to its ignored live state."""

    example: str
    live: str


CHILD_STATE_PAIRS = (
    StatePair(
        ".codex/agents/drowai-tool-capability-analysis-state.example.md",
        ".codex/agents/drowai-tool-capability-analysis-state.md",
    ),
    StatePair(
        ".codex/agents/drowai-tool-mechanical-validation-state.example.md",
        ".codex/agents/drowai-tool-mechanical-validation-state.md",
    ),
    StatePair(
        ".codex/agents/implementation-guide-state.example.md",
        ".codex/agents/implementation-guide-state.md",
    ),
    StatePair(
        ".codex/agents/implementation-guide-review-state.example.md",
        ".codex/agents/implementation-guide-review-state.md",
    ),
    StatePair(
        ".codex/agents/implementation-state.example.md",
        ".codex/agents/implementation-state.md",
    ),
    StatePair(
        ".codex/agents/implementation-review-state.example.md",
        ".codex/agents/implementation-review-state.md",
    ),
    StatePair(
        ".codex/agents/implementation-quality-review-state.example.md",
        ".codex/agents/implementation-quality-review-state.md",
    ),
    StatePair(
        ".codex/agents/refactor-guide-state.example.md",
        ".codex/agents/refactor-guide-state.md",
    ),
)


def extract_yaml_block(content: str) -> str:
    """Extract the first YAML fence, or return plain YAML content."""

    marker = "```yaml"
    if marker not in content:
        block = content
    else:
        _, _, remainder = content.partition(marker)
        block, separator, _ = remainder.partition("```")
        if not separator:
            raise ValueError("unterminated YAML fence")

    lines = block.strip().splitlines()
    if lines and lines[0].strip() == "---":
        lines.pop(0)
    if lines and lines[-1].strip() in {"---", "..."}:
        lines.pop()
    return "\n".join(lines).strip() + "\n"


def load_state(path: Path) -> dict[str, Any]:
    """Load one plain or Markdown-fenced YAML state."""

    payload = yaml.safe_load(extract_yaml_block(path.read_text(encoding="utf-8")))
    if not isinstance(payload, dict):
        raise ValueError("state root must be a mapping")
    return payload


def can_transition(
    current: str,
    target: str,
    *,
    resume_status: str | None = None,
) -> bool:
    """Return whether one delivery status transition is allowed."""

    current_status = str(current or "").strip().upper()
    target_status = str(target or "").strip().upper()
    if current_status not in STATUSES or target_status not in STATUSES:
        return False
    if current_status in {"BLOCKED", "NEEDS_CLARIFICATION"}:
        recorded_resume = str(resume_status or "").strip().upper()
        return (
            recorded_resume == target_status
            and target_status
            not in {
                "BLOCKED",
                "NEEDS_CLARIFICATION",
                "MERGED",
                "DECISION_RECORDED",
            }
        )
    if target_status in {"BLOCKED", "NEEDS_CLARIFICATION"}:
        return current_status not in {"MERGED", "DECISION_RECORDED"}
    return target_status in NORMAL_TRANSITIONS.get(current_status, set())


def can_start_new_tool(
    state: Mapping[str, Any],
    *,
    open_delivery_prs: int = 0,
) -> bool:
    """Return whether state and GitHub allow another tool to start."""

    status = str(state.get("status") or "").strip().upper()
    if open_delivery_prs != 0:
        return False
    return status == "READY" or (
        status in {"MERGED", "DECISION_RECORDED"}
        and state.get("next_tool_allowed") is True
    )


def validate_delivery_state(state: Mapping[str, Any]) -> list[str]:
    """Return stable contract error codes for one delivery state."""

    errors: list[str] = []
    status = str(state.get("status") or "").strip().upper()
    if state.get("schema_version") != 1:
        errors.append("schema_version")
    if status not in STATUSES:
        errors.append("status")
    if state.get("one_tool_only") is not True:
        errors.append("one_tool_only")

    next_allowed = state.get("next_tool_allowed")
    if status in {"MERGED", "DECISION_RECORDED"}:
        if next_allowed is not True:
            errors.append("next_tool_allowed")
    elif next_allowed is not False:
        errors.append("next_tool_allowed")

    quality = _mapping(state.get("quality"))
    refactor_round = quality.get("refactor_round")
    max_rounds = quality.get("max_refactor_rounds")
    if not isinstance(refactor_round, int) or not isinstance(max_rounds, int):
        errors.append("quality.refactor_rounds")
    elif refactor_round < 0 or max_rounds != 1 or refactor_round > max_rounds:
        errors.append("quality.refactor_rounds")

    gates = _mapping(state.get("validation_gates"))
    security = _mapping(state.get("static_security_review"))
    if gates.get("static_security") == "passed":
        if not str(security.get("report_ref") or "").strip():
            errors.append("static_security_review.report_ref")
        if not str(security.get("conclusion") or "").strip():
            errors.append("static_security_review.conclusion")
        findings = security.get("blocking_findings")
        if not isinstance(findings, list) or findings:
            errors.append("static_security_review.blocking_findings")

    if status in {"READY_FOR_PR", "PR_OPENED", "AWAITING_MERGE", "MERGED"}:
        for gate in REQUIRED_PR_GATES:
            if gates.get(gate) != "passed":
                errors.append(f"validation_gates.{gate}")

    external = _mapping(state.get("external_actions"))
    if status == "DECISION_RECORDED" and external.get("decision_recorded") is not True:
        errors.append("external_actions.decision_recorded")

    pr = _mapping(state.get("pr"))
    if status in {"PR_OPENED", "AWAITING_MERGE", "MERGED"}:
        if not isinstance(pr.get("number"), int) or pr.get("number", 0) <= 0:
            errors.append("pr.number")
        if not str(pr.get("url") or "").startswith("https://"):
            errors.append("pr.url")
    if status == "MERGED" and str(pr.get("status") or "").lower() != "merged":
        errors.append("pr.status")

    return sorted(set(errors))


def _mapping(value: Any) -> Mapping[str, Any]:
    """Return a mapping view or an empty mapping."""

    return value if isinstance(value, Mapping) else {}


def _safe_live_path(repo_root: Path, relative_path: str) -> Path:
    """Resolve and constrain one live state path to `.codex/agents`."""

    agents_root = (repo_root / ".codex/agents").resolve()
    target = (repo_root / relative_path).resolve()
    try:
        target.relative_to(agents_root)
    except ValueError as exc:
        raise ValueError("live state path escaped .codex/agents") from exc
    return target


def reset_child_states(
    repo_root: Path,
    *,
    apply: bool,
    state_pairs: Sequence[StatePair] = CHILD_STATE_PAIRS,
) -> list[str]:
    """Preview or reset only the explicitly listed child workflow states."""

    actions: list[str] = []
    for pair in state_pairs:
        example_path = (repo_root / pair.example).resolve()
        live_path = _safe_live_path(repo_root, pair.live)
        rendered = extract_yaml_block(example_path.read_text(encoding="utf-8"))
        yaml.safe_load(rendered)
        actions.append(pair.live)
        if apply:
            live_path.parent.mkdir(parents=True, exist_ok=True)
            live_path.write_text(rendered, encoding="utf-8")
    return actions


def cleanup_child_states(
    repo_root: Path,
    *,
    apply: bool,
    state_pairs: Sequence[StatePair] = CHILD_STATE_PAIRS,
) -> list[str]:
    """Preview or remove only the explicitly listed ignored child states."""

    actions: list[str] = []
    for pair in state_pairs:
        live_path = _safe_live_path(repo_root, pair.live)
        if not live_path.exists():
            continue
        actions.append(pair.live)
        if apply:
            live_path.unlink()
    return actions


def cleanup_workflow_states(
    repo_root: Path,
    *,
    apply: bool,
    state_pairs: Sequence[StatePair] = CHILD_STATE_PAIRS,
) -> list[str]:
    """Preview or remove the explicit child states and delivery ledger."""

    actions = cleanup_child_states(
        repo_root,
        apply=apply,
        state_pairs=state_pairs,
    )
    delivery_path = _safe_live_path(repo_root, DELIVERY_LIVE_STATE)
    if delivery_path.exists():
        actions.append(DELIVERY_LIVE_STATE)
        if apply:
            delivery_path.unlink()
    return actions


def build_parser() -> argparse.ArgumentParser:
    """Build the state-management command parser."""

    parser = argparse.ArgumentParser(description="Manage one-tool delivery state.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate")
    validate.add_argument("--state", required=True, type=Path)

    transition = subparsers.add_parser("check-transition")
    transition.add_argument("--from-status", required=True)
    transition.add_argument("--to-status", required=True)
    transition.add_argument("--resume-status")

    for name in (
        "reset-child-states",
        "cleanup-child-states",
        "cleanup-workflow-states",
    ):
        command = subparsers.add_parser(name)
        command.add_argument("--repo-root", type=Path, default=Path.cwd())
        command.add_argument("--apply", action="store_true")
        command.add_argument("--confirm-no-active-workflow", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one validation, transition check, reset, or cleanup action."""

    args = build_parser().parse_args(argv)
    if args.command == "validate":
        errors = validate_delivery_state(load_state(args.state))
        if errors:
            print("[tool-delivery-state] FAIL")
            for error in errors:
                print(f"- {error}")
            return 1
        print("[tool-delivery-state] PASS")
        return 0
    if args.command == "check-transition":
        allowed = can_transition(
            args.from_status,
            args.to_status,
            resume_status=args.resume_status,
        )
        print("[tool-delivery-state] ALLOWED" if allowed else "[tool-delivery-state] INVALID")
        return 0 if allowed else 1

    if args.apply and not args.confirm_no_active_workflow:
        print("[tool-delivery-state] --confirm-no-active-workflow is required with --apply")
        return 1
    repo_root = args.repo_root.resolve()
    if args.command == "reset-child-states":
        actions = reset_child_states(repo_root, apply=args.apply)
    elif args.command == "cleanup-child-states":
        actions = cleanup_child_states(repo_root, apply=args.apply)
    else:
        actions = cleanup_workflow_states(repo_root, apply=args.apply)
    mode = "applied" if args.apply else "preview"
    print(f"[tool-delivery-state] {mode}: {len(actions)} explicit child states")
    for action in actions:
        print(f"- {action}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
