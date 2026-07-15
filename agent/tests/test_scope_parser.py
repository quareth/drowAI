"""Tests for the enhanced ScopeParser implementation."""

import pytest
import tempfile
import os
from typing import List

try:
    from ..planner import ScopeParser
    from ..models import ParsedScope, Target, Constraint
except ImportError:
    import sys
    sys.path.append(os.path.dirname(os.path.dirname(__file__)))
    from planner import ScopeParser
    from models import ParsedScope, Target, Constraint


class TestScopeParser:
    """Test cases for the enhanced ScopeParser."""
    
    def setup_method(self):
        """Set up test fixtures."""
        self.parser = ScopeParser()
    
    def test_parse_complete_scope_document(self):
        """Test parsing a complete scope document with all sections."""
        content = """# Penetration Testing Scope

## Targets
- 192.168.1.0/24
- webapp.example.com  
- https://api.example.com
- 10.0.0.1-10.0.0.50

## Objectives
- Network reconnaissance
- Web application testing
- API security assessment
- Database security review

## Constraints
- No DoS attacks
- Business hours only: 9 AM - 5 PM EST
- Avoid production database servers
- Rate limit: max 10 requests/second

## Methodology
- OWASP Top 10 testing
- Network port scanning
- Web vulnerability scanning
- Social engineering (email only)

## Time Limit
- Maximum 4 hours

## Output Format
- Detailed markdown report
- Executive summary
- Technical findings with evidence
"""
        
        result = self.parser.parse_markdown_content(content)
        
        # Verify targets
        assert len(result.targets) == 4
        assert result.targets[0].type == 'cidr'
        assert result.targets[0].normalized == '192.168.1.0/24'
        assert result.targets[1].type == 'domain'
        assert result.targets[1].normalized == 'webapp.example.com'
        assert result.targets[2].type == 'url'
        assert result.targets[2].normalized == 'https://api.example.com'
        assert result.targets[3].type == 'ip_range'
        assert result.targets[3].normalized == '10.0.0.1-10.0.0.50'
        
        # Verify objectives
        assert len(result.objectives) == 4
        assert 'Network reconnaissance' in result.objectives
        assert 'Web application testing' in result.objectives
        
        # Verify constraints
        assert len(result.constraints) == 4
        dos_constraint = next(c for c in result.constraints if 'DoS' in c.raw)
        assert dos_constraint.type == 'exclusion'
        
        rate_constraint = next(c for c in result.constraints if 'rate limit' in c.raw.lower())
        assert rate_constraint.type == 'rate_limit'
        assert rate_constraint.details['requests'] == 10
        assert rate_constraint.details['per'] == 'second'
        
        # Verify methodology
        assert len(result.methodology) == 4
        assert 'OWASP Top 10 testing' in result.methodology
        
        # Verify time limit (4 hours = 240 minutes)
        assert result.time_limit == 240
        
        # Verify output format
        assert result.output_format == 'markdown'
        
        # Should have no validation errors
        assert not self.parser.has_errors()
    
    def test_parse_various_list_formats(self):
        """Test parsing different markdown list formats."""
        content = """## Targets
- 192.168.1.1
* 192.168.1.2
1. 192.168.1.3
2) 192.168.1.4

## Objectives
- Bullet point objective
* Asterisk objective
1. Numbered objective
2) Parenthesis objective

## Constraints
| Constraint Type | Description |
|-----------------|-------------|
| Timing | Business hours only |
| Method | No destructive tests |
"""
        
        result = self.parser.parse_markdown_content(content)
        
        # Should parse all target formats
        assert len(result.targets) == 4
        assert all(t.type == 'ip' for t in result.targets)
        
        # Should parse all objective formats
        assert len(result.objectives) == 4
        
        # Should parse table format constraints
        assert len(result.constraints) >= 2
    
    def test_target_type_classification(self):
        """Test target type classification accuracy."""
        test_cases = [
            ('192.168.1.1', 'ip'),
            ('192.168.1.0/24', 'cidr'),
            ('192.168.1.1-192.168.1.50', 'ip_range'),
            ('example.com', 'domain'),
            ('sub.example.com', 'domain'),
            ('https://example.com', 'url'),
            ('http://api.example.com/v1', 'url'),
            ('invalid-target-format!@#', 'unknown'),
        ]
        
        for target_str, expected_type in test_cases:
            target = Target.parse(target_str)
            assert target.type == expected_type, f"Failed for {target_str}: expected {expected_type}, got {target.type}"
    
    def test_constraint_type_classification(self):
        """Test constraint type classification."""
        test_cases = [
            ('Business hours only: 9 AM - 5 PM', 'timing'),
            ('No DoS attacks allowed', 'exclusion'),
            ('Rate limit: max 10 requests/second', 'rate_limit'),
            ('Avoid production systems', 'exclusion'),
            ('General testing constraint', 'general'),
        ]
        
        for constraint_str, expected_type in test_cases:
            constraint = Constraint.parse(constraint_str)
            assert constraint.type == expected_type, f"Failed for {constraint_str}: expected {expected_type}, got {constraint.type}"
    
    def test_time_limit_parsing(self):
        """Test time limit parsing from various formats."""
        test_cases = [
            (['Maximum 4 hours'], 240),
            (['2 hours'], 120),
            (['90 minutes'], 90),
            (['Maximum 6 hours'], 360),
            (['No time specified'], 240),  # Should use default
        ]
        
        for time_strings, expected_minutes in test_cases:
            result = self.parser._parse_time_limit(time_strings)
            assert result == expected_minutes
    
    def test_testing_depth_determination(self):
        """Test testing depth classification."""
        test_cases = [
            (['comprehensive testing'], ['detailed methodology'], 'comprehensive'),
            (['surface scan'], ['basic checks'], 'surface'),
            (['thorough analysis'], ['complete testing'], 'comprehensive'),
            (['detailed review'], ['full assessment'], 'deep'),
            (['basic scan'], ['simple check'], 'surface'),
        ]
        
        for objectives, methodology, expected_depth in test_cases:
            result = self.parser._determine_testing_depth(objectives, methodology)
            assert result == expected_depth
    
    def test_file_parsing(self):
        """Test parsing from actual file."""
        content = """## Targets
- 192.168.1.1
- example.com

## Objectives
- Basic security scan
"""
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as f:
            f.write(content)
            temp_file = f.name
        
        try:
            result = self.parser.parse_scope_file(temp_file)
            assert len(result.targets) == 2
            assert len(result.objectives) == 1
        finally:
            os.unlink(temp_file)
    
    def test_missing_file_error(self):
        """Test error handling for missing files."""
        with pytest.raises(FileNotFoundError):
            self.parser.parse_scope_file('/nonexistent/file.md')
    
    def test_empty_file_error(self):
        """Test error handling for empty files."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as f:
            f.write('')
            temp_file = f.name
        
        try:
            with pytest.raises(ValueError, match="Scope file is empty"):
                self.parser.parse_scope_file(temp_file)
        finally:
            os.unlink(temp_file)
    
    def test_validation_errors(self):
        """Test validation error collection."""
        content = """## Targets
- 999.999.999.999
- invalid-domain!@#
- https://invalid-url with spaces

## Objectives
- Valid objective

## Constraints
- Valid constraint
"""
        
        result = self.parser.parse_markdown_content(content)
        
        # Should have validation errors for invalid targets
        assert self.parser.has_errors()
        errors = self.parser.get_validation_errors()
        assert len(errors) > 0
        assert any('Invalid IP address' in error for error in errors)
    
    def test_no_targets_error(self):
        """Test error when no valid targets are found."""
        content = """## Objectives
- Some objective

## Constraints
- Some constraint
"""
        
        result = self.parser.parse_markdown_content(content)
        
        # Should have validation error for missing targets
        assert self.parser.has_errors()
        errors = self.parser.get_validation_errors()
        assert any('No valid targets found' in error for error in errors)
    
    def test_warnings_collection(self):
        """Test warning collection for minor issues."""
        content = """## Targets
- 192.168.1.1
- some-unknown-format-target

## Constraints
- Valid constraint
"""
        
        result = self.parser.parse_markdown_content(content)
        
        # Should have warnings for unknown target format
        warnings = self.parser.get_warnings()
        assert len(warnings) > 0
        assert any('Unknown target format' in warning for warning in warnings)
    
    def test_ip_address_validation(self):
        """Test IP address validation edge cases."""
        test_cases = [
            ('192.168.1.1', True),
            ('0.0.0.0', True),
            ('255.255.255.255', True),
            ('192.168.1.256', False),
            ('192.168', False),
            ('192.168.1.1.1', False),
            ('abc.def.ghi.jkl', False),
        ]
        
        for ip, expected_valid in test_cases:
            result = self.parser._validate_ip_address(ip)
            assert result == expected_valid, f"Failed for {ip}: expected {expected_valid}, got {result}"
    
    def test_domain_validation(self):
        """Test domain validation edge cases."""
        test_cases = [
            ('example.com', True),
            ('sub.example.com', True),
            ('a.b.c.d.example.com', True),
            ('localhost', True),
            ('', False),
            ('example.', False),
            ('.example.com', False),
            ('example..com', False),
            ('a' * 256, False),  # Too long
        ]
        
        for domain, expected_valid in test_cases:
            result = self.parser._validate_domain(domain)
            assert result == expected_valid, f"Failed for {domain}: expected {expected_valid}, got {result}"
    
    def test_url_validation(self):
        """Test URL validation."""
        test_cases = [
            ('https://example.com', True),
            ('http://example.com', True),
            ('https://api.example.com/v1/test', True),
            ('ftp://example.com', False),
            ('not-a-url', False),
            ('https://', False),
        ]
        
        for url, expected_valid in test_cases:
            result = self.parser._validate_url(url)
            assert result == expected_valid, f"Failed for {url}: expected {expected_valid}, got {result}"
    
    def test_to_dict_serialization(self):
        """Test ParsedScope to_dict serialization."""
        content = """## Targets
- 192.168.1.1

## Objectives
- Test objective

## Constraints
- Test constraint
"""
        
        result = self.parser.parse_markdown_content(content)
        data = result.to_dict()
        
        # Verify structure
        assert 'targets' in data
        assert 'objectives' in data
        assert 'constraints' in data
        assert 'methodology' in data
        assert 'time_limit' in data
        assert 'testing_depth' in data
        assert 'output_format' in data
        
        # Verify target serialization
        assert len(data['targets']) == 1
        assert data['targets'][0]['type'] == 'ip'
        assert data['targets'][0]['normalized'] == '192.168.1.1'
        
        # Verify constraint serialization
        assert len(data['constraints']) == 1
        assert 'type' in data['constraints'][0]
        assert 'details' in data['constraints'][0]

    def test_parse_scope_document_model(self, tmp_path):
        """Test new parse_scope_document method."""
        content = """## Targets
- 10.1.1.1

## Objectives
- Sample objective

## Constraints
- No DoS attacks
- Rate limit: 5 requests/minute

## Methodology
- Basic scan

## Business Hours
- 9 AM - 5 PM

## Output Format
- Markdown
"""
        file_path = tmp_path / "scope.md"
        file_path.write_text(content)

        result = self.parser.parse_scope_document(str(file_path))
        assert result.targets == ["10.1.1.1"]
        assert "No DoS" in result.constraints[0]
        assert result.methodology == ["Basic scan"]
        assert result.business_hours == "9 AM - 5 PM"
        assert result.rate_limits["requests"] == 5


if __name__ == '__main__':
    pytest.main([__file__])
