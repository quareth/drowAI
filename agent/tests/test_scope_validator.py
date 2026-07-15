import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

try:
    from agent.scope_validator import ScopeValidator
    from agent.models import ScopeDocument, SecurityContext
    from agent.logger import AgentLogger
except Exception:
    from scope_validator import ScopeValidator
    from models import ScopeDocument, SecurityContext
    from agent.logger import AgentLogger


def make_validator(**kwargs):
    scope = ScopeDocument(
        targets=kwargs.get('targets', ['192.168.1.1']),
        objectives=[],
        constraints=kwargs.get('constraints', []),
        methodology=[],
        time_limit=None,
        business_hours=kwargs.get('business_hours'),
        rate_limits=kwargs.get('rate_limits', {}),
        output_format=[],
        security=kwargs.get('security') or SecurityContext.parse(kwargs.get('constraints', [])),
    )
    logger = AgentLogger(task_id='val-test')
    return ScopeValidator(scope, logger)


def test_target_out_of_scope():
    validator = make_validator()
    result = validator.validate_proposed_action('ping', '10.0.0.1')
    assert not result.is_valid
    assert any('out of scope' in e.lower() for e in result.errors)


def test_dos_action_blocked():
    validator = make_validator(constraints=['No DoS attacks'])
    result = validator.validate_proposed_action('run dos attack', '192.168.1.1')
    assert not result.is_valid
    assert any('dos' in e.lower() for e in result.errors)


def test_business_hours_enforced():
    validator = make_validator(business_hours='9 AM - 5 PM')
    validator._current_time = lambda: datetime(2020,1,1,8,0,0)
    result = validator.validate_proposed_action('ping', '192.168.1.1')
    assert not result.is_valid
    assert any('business hours' in e.lower() for e in result.errors)


def test_rate_limit():
    validator = make_validator(rate_limits={'requests': 1, 'per': 'second'})
    validator._current_time = lambda: datetime(2020,1,1,10,0,0)
    result1 = validator.validate_proposed_action('ping', '192.168.1.1')
    assert result1.is_valid
    result2 = validator.validate_proposed_action('ping', '192.168.1.1')
    assert not result2.is_valid
    assert any('rate limit' in e.lower() for e in result2.errors)


def test_ip_blacklist():
    validator = make_validator(constraints=['Blacklist 10.0.0.5'], targets=['10.0.0.5'])
    result = validator.validate_proposed_action('ping', '10.0.0.5')
    assert not result.is_valid
    assert any('blacklisted' in e.lower() for e in result.errors)


def test_ip_whitelist():
    validator = make_validator(constraints=['Allow 10.0.0.5'], targets=['10.0.0.6'])
    result = validator.validate_proposed_action('ping', '10.0.0.6')
    assert not result.is_valid
    assert any('not whitelisted' in e.lower() for e in result.errors)


def test_excluded_technique():
    validator = make_validator(constraints=['No brute force'])
    result = validator.validate_proposed_action('perform brute force attack', '192.168.1.1')
    assert not result.is_valid
    assert any('prohibited' in e.lower() for e in result.errors)
