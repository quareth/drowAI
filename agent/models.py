"""Data models and enums for the penetration testing agent."""

from __future__ import annotations

from dataclasses import dataclass, asdict, field
from enum import Enum
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional, Dict, Any
import re

# ExecutionStrategy is owned by the leaf module to break the
# ``agent.models`` ↔ ``agent.tool_runtime.batch.types`` import cycle.
# Re-exported here so existing ``from agent.models import ExecutionStrategy``
# call sites keep working unchanged. See Task 1.1.5.
from agent.execution_strategy import ExecutionStrategy

if TYPE_CHECKING:
    # Forward-only reference: the value type lives in the batch package,
    # but ``agent/models.py`` only needs the name at type-check time so
    # the runtime import graph stays acyclic.
    from agent.tool_runtime.batch.types import ToolBatch


@dataclass
class Target:
    """Represents a parsed target with type classification."""
    
    raw: str  # Original string from scope document
    type: str  # 'ip', 'ip_range', 'domain', 'url', 'cidr'
    normalized: str  # Cleaned/validated version
    port: Optional[int] = None
    protocol: Optional[str] = None
    
    @classmethod
    def parse(cls, target_str: str) -> "Target":
        """Parse a target string and classify its type."""
        target_str = target_str.strip()
        
        # URL pattern (http/https)
        if re.match(r'^https?://', target_str):
            return cls(raw=target_str, type='url', normalized=target_str)
        
        # IP range pattern (192.168.1.1-192.168.1.50)
        if re.match(r'^\d+\.\d+\.\d+\.\d+-\d+\.\d+\.\d+\.\d+$', target_str):
            return cls(raw=target_str, type='ip_range', normalized=target_str)
        
        # CIDR pattern (192.168.1.0/24)
        if re.match(r'^\d+\.\d+\.\d+\.\d+/\d+$', target_str):
            return cls(raw=target_str, type='cidr', normalized=target_str)
        
        # Single IP pattern
        if re.match(r'^\d+\.\d+\.\d+\.\d+$', target_str):
            return cls(raw=target_str, type='ip', normalized=target_str)
        
        # Domain pattern
        if re.match(r'^[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+$', target_str):
            return cls(raw=target_str, type='domain', normalized=target_str.lower())
        
        # Default to unknown type
        return cls(raw=target_str, type='unknown', normalized=target_str)


@dataclass
class Constraint:
    """Represents a testing constraint or limitation."""
    
    raw: str  # Original constraint text
    type: str  # 'timing', 'method', 'rate_limit', 'exclusion', 'general'
    details: Dict[str, Any]  # Parsed constraint details
    
    @classmethod
    def parse(cls, constraint_str: str) -> "Constraint":
        """Parse a constraint string and extract details."""
        constraint_str = constraint_str.strip()
        lower_str = constraint_str.lower()
        
        # Time-based constraints
        if any(word in lower_str for word in ['hours', 'time', 'am', 'pm', 'business']):
            return cls(
                raw=constraint_str,
                type='timing',
                details={'description': constraint_str}
            )
        
        # Rate limiting constraints
        if any(word in lower_str for word in ['rate', 'limit', 'requests', 'second', 'minute']):
            # Try to extract rate limit numbers
            rate_match = re.search(r'(\d+)\s*requests?/(\w+)', lower_str)
            if rate_match:
                return cls(
                    raw=constraint_str,
                    type='rate_limit',
                    details={
                        'requests': int(rate_match.group(1)),
                        'per': rate_match.group(2),
                        'description': constraint_str
                    }
                )
            return cls(raw=constraint_str, type='rate_limit', details={'description': constraint_str})
        
        # Method exclusions (DoS, destructive tests)
        if any(word in lower_str for word in ['no dos', 'avoid', 'exclude', 'don\'t', 'not']):
            return cls(
                raw=constraint_str,
                type='exclusion',
                details={'description': constraint_str}
            )
        
        # General constraint
        return cls(
            raw=constraint_str,
            type='general',
            details={'description': constraint_str}
        )


@dataclass
class SecurityContext:
    """Parsed security constraints for tool execution validation."""

    ip_whitelist: List[str] = field(default_factory=list)
    ip_blacklist: List[str] = field(default_factory=list)
    domain_whitelist: List[str] = field(default_factory=list)
    domain_blacklist: List[str] = field(default_factory=list)
    excluded_techniques: List[str] = field(default_factory=list)
    rate_limits: Dict[str, Any] = field(default_factory=dict)

    IP_RE = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,2})?)")
    IP_RANGE_RE = re.compile(
        r"(\d{1,3}(?:\.\d{1,3}){3})\s*-\s*(\d{1,3}(?:\.\d{1,3}){3})"
    )
    DOMAIN_RE = re.compile(r"(\*?\.?[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})")
    RATE_RE = re.compile(r"(\d+)\s*requests?/(second|minute|hour)")

    @classmethod
    def parse(cls, constraints: List[str]) -> "SecurityContext":
        ctx = cls()
        for c in constraints:
            lower = c.lower()
            # IP ranges
            for start, end in cls.IP_RANGE_RE.findall(c):
                rng = f"{start}-{end}"
                if any(k in lower for k in ["deny", "block", "blacklist"]):
                    ctx.ip_blacklist.append(rng)
                elif any(k in lower for k in ["allow", "whitelist", "permit"]):
                    ctx.ip_whitelist.append(rng)
            # single IP or CIDR
            for ip in cls.IP_RE.findall(c):
                if "-" in ip:
                    continue
                if any(k in lower for k in ["deny", "block", "blacklist"]):
                    ctx.ip_blacklist.append(ip)
                elif any(k in lower for k in ["allow", "whitelist", "permit"]):
                    ctx.ip_whitelist.append(ip)
            # domains
            for domain in cls.DOMAIN_RE.findall(c):
                if any(ch.isdigit() for ch in domain):
                    # crude check to avoid IPs mistaken as domains
                    pass
                if any(k in lower for k in ["deny", "block", "blacklist"]):
                    ctx.domain_blacklist.append(domain)
                elif any(k in lower for k in ["allow", "whitelist", "permit"]):
                    ctx.domain_whitelist.append(domain)
            # excluded techniques
            if any(k in lower for k in ["no", "avoid", "exclude", "prohibited"]):
                if "dos" in lower:
                    ctx.excluded_techniques.append("dos")
                m = re.search(r"(?:no|avoid|exclude|prohibited)\s+([\w\s-]+)", lower)
                if m:
                    tech = m.group(1).strip().strip('.')
                    if tech and tech not in ctx.excluded_techniques:
                        ctx.excluded_techniques.append(tech)
            # rate limits
            m = cls.RATE_RE.search(lower)
            if m:
                ctx.rate_limits = {"requests": int(m.group(1)), "per": m.group(2)}
        return ctx



@dataclass
class ParsedScope:
    """Enhanced parsed scope document with structured data."""
    
    targets: List[Target]
    objectives: List[str]
    constraints: List[Constraint]
    methodology: List[str]
    time_limit: int = 240  # Default 4 hours in minutes
    testing_depth: str = "surface"
    output_format: str = "markdown"
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            'targets': [{'raw': t.raw, 'type': t.type, 'normalized': t.normalized} for t in self.targets],
            'objectives': self.objectives,
            'constraints': [{'raw': c.raw, 'type': c.type, 'details': c.details} for c in self.constraints],
            'methodology': self.methodology,
            'time_limit': self.time_limit,
            'testing_depth': self.testing_depth,
            'output_format': self.output_format
        }


@dataclass
class ScopeDocument:
    """Structured scope document with helper query methods."""

    targets: List[str]
    objectives: List[str]
    constraints: List[str]
    methodology: List[str]
    time_limit: Optional[str] = None
    business_hours: Optional[str] = None
    rate_limits: Dict[str, Any] = field(default_factory=dict)
    output_format: List[str] = field(default_factory=list)
    testing_depth: str = "surface"
    security: SecurityContext = field(default_factory=SecurityContext)

    @classmethod
    def from_markdown(cls, content: str) -> "ScopeDocument":
        """Parse a small markdown scope file for backward compatibility."""
        sections: Dict[str, List[str]] = {}
        current: Optional[str] = None

        for line in content.splitlines():
            stripped = line.strip()
            lower = stripped.lower()
            if lower.startswith("## "):
                header = lower[3:].strip()
                if header.startswith("targets"):
                    current = "targets"
                elif header.startswith("objectives"):
                    current = "objectives"
                elif header.startswith("constraints"):
                    current = "constraints"
                elif header.startswith("methodology"):
                    current = "methodology"
                elif header.startswith("time limit"):
                    current = "time_limit"
                elif header.startswith("business hours"):
                    current = "business_hours"
                elif header.startswith("output format"):
                    current = "output_format"
                else:
                    current = None
            elif stripped.startswith("-") and current:
                sections.setdefault(current, []).append(stripped[1:].strip())

        return cls(
            targets=sections.get("targets", []),
            objectives=sections.get("objectives", []),
            constraints=sections.get("constraints", []),
            methodology=sections.get("methodology", []),
            time_limit=sections.get("time_limit", [None])[0],
            business_hours=sections.get("business_hours", [None])[0],
            rate_limits={},
            output_format=sections.get("output_format", []),
            security=SecurityContext.parse(sections.get("constraints", [])),
        )
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            'targets': self.targets,
            'objectives': self.objectives,
            'constraints': self.constraints,
            'methodology': self.methodology,
            'time_limit': self.time_limit,
            'business_hours': self.business_hours,
            'rate_limits': self.rate_limits,
            'output_format': self.output_format,
            'testing_depth': self.testing_depth,
            'security': asdict(self.security),
        }

    # New query helper methods
    def get_targets_for_phase(self, phase: str) -> List[str]:
        """Return targets relevant for the given phase. Placeholder implementation."""
        return self.targets

    def is_action_allowed(self, action: str, target: str) -> tuple[bool, str]:
        """Check action against parsed security constraints."""
        lowered = action.lower()
        for blk in self.security.ip_blacklist:
            if target == blk:
                return False, f"Target {target} is blacklisted"
        if self.security.ip_whitelist and target not in self.security.ip_whitelist:
            return False, f"Target {target} not in allowed list"
        for blk in self.security.domain_blacklist:
            if target.endswith(blk.lstrip('*')):
                return False, f"Domain {target} is blacklisted"
        if self.security.domain_whitelist and not any(target.endswith(d.lstrip('*')) for d in self.security.domain_whitelist):
            if '.' in target:
                return False, f"Domain {target} not allowed"
        for tech in self.security.excluded_techniques:
            if tech.lower() in lowered:
                return False, f"Technique '{tech}' prohibited by scope"
        if 'dos' in lowered and 'dos' in ' '.join(self.constraints).lower():
            return False, 'DoS attacks not allowed by scope'
        return True, ''

    def get_relevant_constraints(self, action_type: str) -> List[str]:
        """Return constraints that mention the action type."""
        return [c for c in self.constraints if action_type.lower() in c.lower()]


@dataclass
class Finding:
    id: str
    severity: str
    title: str
    description: str
    target: str
    evidence: str
    recommendation: str
    cvss_score: Optional[float] = None
    references: Optional[List[str]] = None


@dataclass
class AgentStatus:
    task_id: str
    phase: str
    progress: int
    current_action: str
    findings: List[Finding]
    errors: List[str]
    estimated_completion: int
    last_updated: datetime

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CommandResult:
    command: List[str]
    stdout: str
    stderr: str
    returncode: int
    execution_time: float
    timestamp: datetime


@dataclass
class ExecutionResult:
    """Result from executing a command asynchronously."""

    success: bool
    stdout: str
    stderr: str
    exit_code: int


class ActionType(Enum):
    SCAN_PORTS = "scan_ports"
    SCAN_WEB = "scan_web"
    ENUMERATE_SERVICES = "enumerate_services"
    TEST_EXPLOIT = "test_exploit"
    GATHER_INFO = "gather_info"
    GENERATE_REPORT = "generate_report"
    END = "end_task"


@dataclass
class Action:
    type: ActionType
    target: str
    parameters: Dict[str, Any]
    reasoning: str
    expected_outcome: str
    command: str = ""
    description: str = ""
    # Enhanced planning fields (optional for backward compatibility)
    selected_tools: List[str] = field(default_factory=list)
    tool_parameters: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    execution_strategy: Optional[ExecutionStrategy] = None


@dataclass
class ActionPlan:
    """Planner-produced, fully specified execution plan.

    Encapsulates the action decision along with concrete tool choices,
    per-tool parameters, and the execution strategy to be used by the
    executor. This allows separation of concerns between planning and
    execution while remaining backward compatible with existing flows.
    """

    type: ActionType
    target: str
    selected_tools: List[str]
    tool_parameters: Dict[str, Dict[str, Any]]
    llm_tool_parameters: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    execution_strategy: ExecutionStrategy = ExecutionStrategy.PARALLEL
    reasoning: str = ""
    expected_outcome: str = ""
    # Token usage records from LLM calls during planning (Phase 7)
    usage_records: List[Dict[str, Any]] = field(default_factory=list)
    # Selector candidate set used by runtime admission to ensure the builder
    # only committed tools from the backend-authorized candidate policy.
    candidate_tools: List[str] = field(default_factory=list)
    # Optional batch produced by the builder. This is the canonical ordered
    # execution contract; selected_tools/tool_parameters remain legacy
    # projections for single-call readers and cannot represent duplicate
    # calls to the same tool.
    tool_batch: Optional["ToolBatch"] = None
