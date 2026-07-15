"""Scope document parser and planning utilities."""

import re
import os
from typing import List, Optional, Dict, Any

try:
    from .models import (
        Action,
        ActionType,
        ScopeDocument,
        ParsedScope,
        Target,
        Constraint,
        SecurityContext,
    )
except ImportError:
    from models import (
        Action,
        ActionType,
        ScopeDocument,
        ParsedScope,
        Target,
        Constraint,
        SecurityContext,
    )


class ScopeParser:
    """Enhanced scope document parser with comprehensive markdown support."""
    
    def __init__(self):
        """Initialize the scope parser."""
        self.validation_errors = []
        self.warnings = []

    # Phase 1 addition
    def parse_scope_document(self, file_path: str) -> ScopeDocument:
        """Parse a markdown scope file into a ScopeDocument."""
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Scope file not found: {file_path}")
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()

        sections = self._extract_sections(content)
        targets = self._parse_targets_section(sections.get("targets", []))
        constraints = self._parse_constraints_section(sections.get("constraints", []))
        objectives = sections.get("objectives", [])
        methodology = sections.get("methodology", [])
        time_limit = sections.get("time limit", [None])
        business_hours = sections.get("business hours", [None])
        output_format = sections.get("output format", [])

        rate_limits: Dict[str, Any] = {}
        for c in constraints:
            match = re.search(r"(\d+)\s*requests?/(\w+)", c.lower())
            if match:
                rate_limits = {"requests": int(match.group(1)), "per": match.group(2)}

        return ScopeDocument(
            targets=targets,
            objectives=objectives,
            constraints=constraints,
            methodology=methodology,
            time_limit=time_limit[0] if time_limit else None,
            business_hours=business_hours[0] if business_hours else None,
            rate_limits=rate_limits,
            output_format=output_format,
            security=SecurityContext.parse(constraints),
        )
    
    def parse_scope_file(self, file_path: str = '/workspace/scope.md') -> ParsedScope:
        """
        Parse scope document from file path.
        
        Args:
            file_path: Path to the scope markdown file
            
        Returns:
            ParsedScope object with structured data
            
        Raises:
            FileNotFoundError: If scope file doesn't exist
            ValueError: If scope file is empty or invalid
        """
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Scope file not found: {file_path}")
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception as e:
            raise ValueError(f"Failed to read scope file: {e}")
        
        if not content.strip():
            raise ValueError("Scope file is empty")
        
        return self.parse_markdown_content(content)
    
    def parse_markdown_content(self, content: str) -> ParsedScope:
        """
        Parse markdown content into structured scope data.
        
        Args:
            content: Raw markdown content
            
        Returns:
            ParsedScope object with parsed sections
        """
        self.validation_errors = []
        self.warnings = []
        
        # Parse all sections
        sections = self._extract_sections(content)
        
        # Parse targets with validation
        targets = self._parse_targets(sections.get('targets', []))
        
        # Parse constraints with classification
        constraints = self._parse_constraints(sections.get('constraints', []))
        
        # Extract other sections
        objectives = sections.get('objectives', [])
        methodology = sections.get('methodology', [])
        
        # Parse time limit
        time_limit = self._parse_time_limit(sections.get('time limit', []))
        
        # Parse output format
        output_format = self._parse_output_format(sections.get('output format', []))
        
        # Determine testing depth
        testing_depth = self._determine_testing_depth(objectives, methodology)
        
        # Validate parsed data
        self._validate_parsed_data(targets, objectives, constraints)
        
        return ParsedScope(
            targets=targets,
            objectives=objectives,
            constraints=constraints,
            methodology=methodology,
            time_limit=time_limit,
            testing_depth=testing_depth,
            output_format=output_format
        )
    
    def _extract_sections(self, content: str) -> Dict[str, List[str]]:
        """Extract all markdown sections into a dictionary."""
        sections = {}
        current_section = None
        current_items = []
        
        for line in content.splitlines():
            line = line.strip()
            
            # Check for section headers (## Section Name)
            if line.startswith('## '):
                # Save previous section
                if current_section:
                    sections[current_section] = current_items
                
                # Start new section
                current_section = line[3:].strip().lower()
                current_items = []
            
            # Extract list items (bullets, numbers, tables)
            elif line and current_section:
                item = self._extract_list_item(line)
                if item:
                    current_items.append(item)
        
        # Save last section
        if current_section:
            sections[current_section] = current_items
        
        return sections
    
    def _extract_list_item(self, line: str) -> Optional[str]:
        """Extract content from various list formats."""
        line = line.strip()
        
        # Bullet points (- item, * item)
        if line.startswith('-') or line.startswith('*'):
            return line[1:].strip()
        
        # Numbered lists (1. item, 1) item)
        if re.match(r'^\d+[\.\)]\s', line):
            return re.sub(r'^\d+[\.\)]\s+', '', line)
        
        # Table rows (| item |)
        if line.startswith('|') and line.endswith('|'):
            # Extract table cell content
            cells = [cell.strip() for cell in line[1:-1].split('|')]
            return ' | '.join(cells) if len(cells) > 1 else cells[0]
        
        # Plain text (for sections without list formatting)
        if line and not line.startswith('#'):
            return line

        return None

    def _parse_targets_section(self, content: List[str]) -> List[str]:
        """Normalize and return target strings."""
        targets = []
        for item in content:
            try:
                t = Target.parse(item)
                targets.append(t.normalized)
            except Exception:
                targets.append(item.strip())
        return targets

    def _parse_constraints_section(self, content: List[str]) -> List[str]:
        """Return cleaned constraint strings."""
        constraints = []
        for item in content:
            cleaned = item.strip()
            if cleaned:
                constraints.append(cleaned)
        return constraints
    
    def _parse_targets(self, target_strings: List[str]) -> List[Target]:
        """Parse target strings into Target objects with validation."""
        targets = []
        
        for target_str in target_strings:
            if not target_str.strip():
                continue
            
            try:
                target = Target.parse(target_str)
                targets.append(target)
                
                # Add validation warnings for unknown types
                if target.type == 'unknown':
                    self.warnings.append(f"Unknown target format: {target_str}")
                
            except Exception as e:
                self.validation_errors.append(f"Failed to parse target '{target_str}': {e}")
        
        return targets
    
    def _parse_constraints(self, constraint_strings: List[str]) -> List[Constraint]:
        """Parse constraint strings into Constraint objects."""
        constraints = []
        
        for constraint_str in constraint_strings:
            if not constraint_str.strip():
                continue
            
            try:
                constraint = Constraint.parse(constraint_str)
                constraints.append(constraint)
            except Exception as e:
                self.validation_errors.append(f"Failed to parse constraint '{constraint_str}': {e}")
        
        return constraints
    
    def _parse_time_limit(self, time_strings: List[str]) -> int:
        """Parse time limit from strings, return minutes."""
        default_time = 240  # 4 hours default
        
        for time_str in time_strings:
            time_str = time_str.lower()
            
            # Extract hours
            hour_match = re.search(r'(\d+)\s*hours?', time_str)
            if hour_match:
                return int(hour_match.group(1)) * 60
            
            # Extract minutes
            minute_match = re.search(r'(\d+)\s*minutes?', time_str)
            if minute_match:
                return int(minute_match.group(1))
            
            # Extract "maximum X hours" format
            max_match = re.search(r'maximum\s+(\d+)\s*hours?', time_str)
            if max_match:
                return int(max_match.group(1)) * 60
        
        return default_time
    
    def _parse_output_format(self, format_strings: List[str]) -> str:
        """Parse output format preferences."""
        default_format = "markdown"
        
        for format_str in format_strings:
            format_str = format_str.lower()
            
            if 'pdf' in format_str:
                return "pdf"
            elif 'json' in format_str:
                return "json"
            elif 'html' in format_str:
                return "html"
            elif 'markdown' in format_str or 'md' in format_str:
                return "markdown"
        
        return default_format
    
    def _determine_testing_depth(self, objectives: List[str], methodology: List[str]) -> str:
        """Determine testing depth based on objectives and methodology."""
        all_text = ' '.join(objectives + methodology).lower()
        
        # Look for depth indicators
        if any(word in all_text for word in ['comprehensive', 'thorough', 'deep', 'extensive']):
            return "comprehensive"
        elif any(word in all_text for word in ['detailed', 'complete', 'full']):
            return "deep"
        else:
            return "surface"
    
    def _validate_parsed_data(self, targets: List[Target], objectives: List[str], constraints: List[Constraint]):
        """Validate parsed data and collect errors/warnings."""
        
        # Check for empty targets
        if not targets:
            self.validation_errors.append("No valid targets found in scope document")
        
        # Check for empty objectives
        if not objectives:
            self.warnings.append("No objectives specified - using default testing approach")
        
        # Validate target formats
        for target in targets:
            if target.type == 'ip':
                if not self._validate_ip_address(target.normalized):
                    self.validation_errors.append(f"Invalid IP address: {target.raw}")
            elif target.type == 'domain':
                if not self._validate_domain(target.normalized):
                    self.validation_errors.append(f"Invalid domain: {target.raw}")
            elif target.type == 'url':
                if not self._validate_url(target.normalized):
                    self.validation_errors.append(f"Invalid URL: {target.raw}")
    
    def _validate_ip_address(self, ip: str) -> bool:
        """Validate IP address format."""
        parts = ip.split('.')
        if len(parts) != 4:
            return False
        
        try:
            return all(0 <= int(part) <= 255 for part in parts)
        except ValueError:
            return False
    
    def _validate_domain(self, domain: str) -> bool:
        """Validate domain name format."""
        if not domain or len(domain) > 255:
            return False
        
        # Basic domain regex
        pattern = r'^[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?)*$'
        return bool(re.match(pattern, domain))
    
    def _validate_url(self, url: str) -> bool:
        """Validate URL format."""
        # Basic URL validation
        url_pattern = r'^https?://[^\s/$.?#].[^\s]*$'
        return bool(re.match(url_pattern, url, re.IGNORECASE))
    
    def get_validation_errors(self) -> List[str]:
        """Get validation errors from last parse operation."""
        return self.validation_errors.copy()
    
    def get_warnings(self) -> List[str]:
        """Get warnings from last parse operation."""
        return self.warnings.copy()
    
    def has_errors(self) -> bool:
        """Check if last parse operation had errors."""
        return len(self.validation_errors) > 0


class ScopePlanner:
    """Generates a simple test plan from a scope document."""

    def create_test_plan(self, scope: ScopeDocument):
        actions = []
        for target in scope.targets:
            actions.append(
                Action(
                    type=ActionType.SCAN_PORTS,
                    target=target,
                    parameters={},
                    reasoning="initial scan",
                    expected_outcome="open ports",
                )
            )
        return actions
