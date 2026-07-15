from __future__ import annotations

import re
import ipaddress
from datetime import datetime
from typing import List

try:
    from .models import ScopeDocument
    from .logger import AgentLogger
    from .environment_validator import ValidationResult
except ImportError:  # pragma: no cover
    from models import ScopeDocument
    from logger import AgentLogger
    from environment_validator import ValidationResult


class ScopeValidator:
    """Validate proposed actions against scope constraints."""

    def __init__(self, scope_doc: ScopeDocument, logger: AgentLogger):
        self.scope_doc = scope_doc
        self.logger = logger
        self._request_times: List[datetime] = []
        self.security = getattr(scope_doc, "security", None)

    def validate_proposed_action(self, command: str, target: str) -> ValidationResult:
        """Validate a command and target against the loaded scope."""
        result = ValidationResult(is_valid=True, errors=[], warnings=[])

        # Allow task-ending actions regardless of scope
        if "end" in command.lower() or target.lower() == "none":
            return result

        if not self._is_target_in_scope(target):
            result.add_error(f"Target {target} is out of scope")
            return result

        for violation in self._check_constraint_violations(command):
            result.add_error(violation)

        for violation in self._check_security_violations(command, target):
            result.add_error(violation)

        return result

    def _is_target_in_scope(self, target: str) -> bool:
        """Check if the target is allowed by the scope document."""
        for allowed in self.scope_doc.targets:
            allowed = allowed.strip()
            if not allowed:
                continue
            if allowed.lower() == target.lower():
                return True
            # CIDR notation
            if '/' in allowed:
                try:
                    net = ipaddress.ip_network(allowed, strict=False)
                    if ipaddress.ip_address(target) in net:
                        return True
                except ValueError:
                    pass
            # IP range 1.1.1.1-1.1.1.5
            m = re.match(r'^(\d+\.\d+\.\d+\.\d+)-(\d+\.\d+\.\d+\.\d+)$', allowed)
            if m:
                try:
                    start = ipaddress.ip_address(m.group(1))
                    end = ipaddress.ip_address(m.group(2))
                    tgt = ipaddress.ip_address(target)
                    if start <= tgt <= end:
                        return True
                except ValueError:
                    pass
            # Domain match
            if '.' in allowed and '.' in target and target.lower().endswith(allowed.lower()):
                return True
        return False

    def _check_constraint_violations(self, command: str) -> List[str]:
        """Check command against scope constraints."""
        violations = []
        cmd_lower = command.lower()
        constraint_text = ' '.join(self.scope_doc.constraints).lower()

        if 'no dos' in constraint_text and 'dos' in cmd_lower:
            violations.append('DoS actions are prohibited by scope')

        if self.scope_doc.business_hours and not self._within_business_hours():
            violations.append('Action outside allowed business hours')

        if not self._check_rate_limit():
            violations.append('Rate limit exceeded')

        return violations

    def _check_security_violations(self, command: str, target: str) -> List[str]:
        """Validate against parsed SecurityContext."""
        if not self.security:
            return []
        violations: List[str] = []
        cmd_lower = command.lower()

        # IP blacklist/whitelist
        if target and self.security.ip_blacklist and target in self.security.ip_blacklist:
            violations.append(f"Target {target} is blacklisted")
        if target and self.security.ip_whitelist and target not in self.security.ip_whitelist:
            violations.append(f"Target {target} not whitelisted")

        # Domain lists
        if '.' in target:
            for d in self.security.domain_blacklist:
                if target.endswith(d.lstrip('*')):
                    violations.append(f"Domain {target} is blacklisted")
                    break
            if self.security.domain_whitelist and not any(target.endswith(w.lstrip('*')) for w in self.security.domain_whitelist):
                violations.append(f"Domain {target} not allowed")

        # Technique exclusions
        for tech in self.security.excluded_techniques:
            if tech.lower() in cmd_lower:
                violations.append(f"Technique '{tech}' prohibited by scope")
                break

        # Rate limit check
        if self.security.rate_limits:
            if not self._check_rate_limit(custom=self.security.rate_limits):
                violations.append('Rate limit exceeded')

        return violations

    def _within_business_hours(self) -> bool:
        """Return True if current time is within allowed business hours."""
        bh = self.scope_doc.business_hours
        if not bh:
            return True
        match = re.search(r'(\d+)\s*\w*\s*am\s*-\s*(\d+)\s*\w*\s*pm', bh, re.I)
        if not match:
            return True
        start_hour = int(match.group(1)) % 24
        end_hour = (int(match.group(2)) % 12) + 12
        current_hour = self._current_time().hour
        return start_hour <= current_hour < end_hour

    def _check_rate_limit(self, custom: dict | None = None) -> bool:
        ctx = custom or self.scope_doc.rate_limits
        limit = ctx.get('requests')
        per = ctx.get('per')
        if not limit or not per:
            return True
        seconds = 1
        if per.lower().startswith('minute'):
            seconds = 60
        elif per.lower().startswith('hour'):
            seconds = 3600
        now = self._current_time()
        self._request_times = [t for t in self._request_times if (now - t).total_seconds() < seconds]
        if len(self._request_times) >= limit:
            return False
        self._request_times.append(now)
        return True

    def _current_time(self) -> datetime:
        return datetime.now()
