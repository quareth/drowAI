# Tool Testing Guide

This guide describes the comprehensive automated testing framework for DrowAI pentesting tools.

---

## Quick Start: Testing a New Tool

### Step 1: Create Fixtures

```bash
# Create parameter fixture
cat > tests/tools/fixtures/params/category_subcategory_mytool.json << 'EOF'
{
  "tool_id": "category.subcategory.mytool",
  "test_cases": {
    "minimal": {
      "description": "Required fields only",
      "params": {"target": "192.168.1.1"},
      "expected_valid": true
    },
    "full": {
      "description": "All fields",
      "params": {"target": "192.168.1.1", "port": 80, "timeout": 30},
      "expected_valid": true
    }
  }
}
EOF

# Create output fixture (sample tool output)
cat > tests/tools/fixtures/outputs/category_subcategory_mytool.txt << 'EOF'
Tool output here...
EOF
```

### Step 2: Run Tests for New Tool

```bash
# Check fixtures exist
python -m tests.tools.scripts.test_new_tool --check category.subcategory.mytool

# Run all tests for the tool
python -m tests.tools.scripts.test_new_tool category.subcategory.mytool

# Run with verbose output
python -m tests.tools.scripts.test_new_tool -v category.subcategory.mytool

# Run only schema tests (fastest)
python -m tests.tools.scripts.test_new_tool --schema-only category.subcategory.mytool

# Run only security tests
python -m tests.tools.scripts.test_new_tool --security-only category.subcategory.mytool
```

### Step 3: Alternative pytest Commands

```bash
# Test specific tool by name
python -m pytest tests/tools/contracts/ -k "mytool" -v

# Test specific tool in specific category
python -m pytest tests/tools/contracts/test_information_gathering.py -k "mytool" -v

# Run schema contract only
python -m pytest tests/tools/contracts/ -k "schema_contract and mytool" -v

# Run command contract only
python -m pytest tests/tools/contracts/ -k "command_contract and mytool" -v
```

### Expected Results

A properly implemented tool should pass:
- ✅ Schema contract (args model validation)
- ✅ Command contract (build_command returns valid list)
- ✅ Output contract (parse_output returns dict)
- ✅ Security checks (no injection vulnerabilities)

---

## Overview

The tool testing framework validates:
1. **Schema Contracts** - Validates tool argument schemas
2. **Command Contracts** - Validates CLI command generation
3. **Output Contracts** - Validates output parsing
4. **Command Correctness** - Validates flag syntax and patterns
5. **Value Validation** - Validates input formats (IP, ports, URLs, etc.)
6. **Output Accuracy** - Validates parsed data extraction
7. **Security** - Validates against injection and traversal attacks
8. **Integration** - Validates full execution pipeline with mocks

## Test Categories

### 1. Contract Tests (`tests/tools/contracts/`)

Basic contract validation ensuring tools implement the `BaseTool` interface correctly.

| Test File | Description |
|-----------|-------------|
| `test_information_gathering.py` | Information gathering tools |
| `test_database_assessment.py` | Database assessment tools |
| `test_password_attacks.py` | Password attack tools |
| `test_web_applications.py` | Web application tools |
| `test_command_correctness.py` | Command syntax validation |
| `test_value_validation.py` | Input value validation |
| `test_output_accuracy.py` | Output parsing accuracy |
| `test_security.py` | Security vulnerability checks |

### 2. Integration Tests (`tests/tools/integration/`)

Full pipeline tests using mock execution.

| Test File | Description |
|-----------|-------------|
| `test_mock_execution.py` | Mock execution scenarios |

### 3. Validation Modules (`tests/tools/validation/`)

Reusable validators for different aspects of tool testing.

| Module | Description |
|--------|-------------|
| `schema_validator.py` | Pydantic schema validation |
| `command_validator.py` | CLI command pattern validation |
| `value_validator.py` | Input value format validation |
| `output_validator.py` | Parsed output accuracy validation |
| `security_validator.py` | Security vulnerability detection |

## Running Tests

### Run All Tool Tests

```bash
# All contract tests
python -m pytest tests/tools/contracts/ -v

# All integration tests
python -m pytest tests/tools/integration/ -v

# Everything
python -m pytest tests/tools/ -v
```

### Run by Category

```bash
# Schema contracts only
python -m pytest tests/tools/contracts/ -k "schema_contract" -v

# Command contracts only
python -m pytest tests/tools/contracts/ -k "command_contract" -v

# Security tests only
python -m pytest tests/tools/contracts/test_security.py -v

# Value validation tests
python -m pytest tests/tools/contracts/test_value_validation.py -v
```

### Run for Specific Tool

```bash
# Test specific tool
python -m pytest tests/tools/contracts/ -k "nmap" -v

# Test tool category
python -m pytest tests/tools/contracts/test_information_gathering.py -v
```

## Test Fixtures

### Parameter Fixtures (`tests/tools/fixtures/params/`)

JSON files defining test parameters for each tool:

```json
{
  "tool_id": "information_gathering.network_discovery.nmap",
  "test_cases": {
    "minimal": {
      "description": "Required fields only",
      "params": {"target": "192.168.1.1"},
      "expected_valid": true
    },
    "full": {
      "description": "All optional fields populated",
      "params": {"target": "192.168.1.0/24", "ports": "1-65535", ...},
      "expected_valid": true
    },
    "edge_cases": [...],
    "invalid": [...]
  }
}
```

### Output Fixtures (`tests/tools/fixtures/outputs/`)

Sample tool output for testing parsers:

```
tests/tools/fixtures/outputs/
├── information_gathering_network_discovery_nmap.txt
├── password_attacks_online_attacks_hydra.txt
└── ...
```

### Error Fixtures (`tests/tools/fixtures/error_fixtures.py`)

Standardized error conditions for testing error handling.

## Validation Details

### Command Correctness Validation

The `CommandValidator` checks:

- **Binary name** - Command starts with correct tool name
- **Flag patterns** - Flag values match expected formats
- **Mutual exclusivity** - Conflicting flags aren't used together
- **Required flags** - Mandatory flags are present
- **Positional arguments** - Arguments are in correct positions

Patterns are defined for common tools:
- nmap, hydra, nikto, gobuster, sqlmap, ffuf, amass, masscan, etc.

### Value Validation

The `ValueValidator` validates:

| Value Type | Validation |
|------------|------------|
| IP Address | IPv4/IPv6 format |
| CIDR | Network notation |
| Hostname | RFC 1123 compliance |
| URL | Scheme, host, path |
| Port | 1-65535 range |
| Port Spec | Ranges, lists |
| File Path | Traversal detection |
| Timeout | Reasonable ranges |
| Threads | Valid counts |

### Output Accuracy Validation

The `OutputValidator` checks:

- **Required fields** - Expected fields are present
- **Field types** - Values have correct types
- **List fields** - Collections have expected items
- **Coverage score** - Percentage of expected fields found

### Security Validation

The `SecurityValidator` detects:

| Vulnerability | Detection Method |
|---------------|------------------|
| Command injection | Pattern matching for shell metacharacters |
| Path traversal | Detection of `../` patterns |
| Dangerous commands | Blacklist of risky commands |
| Code patterns | Static analysis for eval/exec |

Injection payloads tested:
- Semicolon chaining: `; ls`
- Pipe injection: `| cat /etc/passwd`
- Command substitution: `` `id` ``, `$(id)`
- Variable expansion: `${IFS}`

## Adding New Tests

### Add Tool to Contract Tests

1. Create parameter fixture: `tests/tools/fixtures/params/{tool_id}.json`
2. Create output fixture: `tests/tools/fixtures/outputs/{tool_id}.txt`
3. Add tool ID to test class `TOOLS` list

### Add Command Pattern

Edit `tests/tools/validation/command_validator.py`:

```python
COMMAND_PATTERNS["mytool"] = CommandPattern(
    tool_name="mytool",
    binary_name="mytool",
    required_flags=["-t"],
    flag_patterns={
        "-t": r"^[\w\.\-]+$",  # target pattern
        "-p": r"^\d+$",        # port pattern
    },
    boolean_flags={"-v", "-q"},
    mutually_exclusive=[{"-v", "-q"}],
)
```

### Add Expected Output Schema

Edit `tests/tools/validation/output_validator.py`:

```python
EXPECTED_OUTPUTS["mytool"] = ExpectedOutput(
    tool_name="mytool",
    required_fields={"results"},
    optional_fields={"hosts", "ports", "services"},
    field_types={
        "results": list,
        "hosts": list,
    },
    list_fields={"results", "hosts"},
)
```

### Add Custom Error Fixture

Edit `tests/tools/fixtures/error_fixtures.py`:

```python
TOOL_ERROR_FIXTURES["mytool"] = {
    "custom_error": ErrorFixture(
        name="custom_error",
        description="Tool-specific error",
        stdout="",
        stderr="Error: Custom error message",
        exit_code=1,
    ),
}
```

## Integration Testing

### Mock Execution

The `MockExecutor` simulates tool execution:

```python
from tests.tools.integration.mock_executor import MockExecutor

executor = MockExecutor()

# Execute with success scenario
result = executor.execute(tool, args, "success")

# Execute with error scenario
result = executor.execute(tool, args, "connection_error")

# Custom scenario
executor.scenarios["custom"] = MockScenario(
    name="custom",
    stdout="Custom output",
    exit_code=0,
)
result = executor.execute(tool, args, "custom")
```

### Scenario Suite

Run all scenarios for a tool:

```python
from tests.tools.integration.mock_executor import IntegrationTestRunner

runner = IntegrationTestRunner()
results = runner.run_scenario_suite(tool, args, tool_id)

summary = runner.get_summary()
print(f"Pass rate: {summary['pass_rate']:.1%}")
```

## Test Coverage Goals

| Aspect | Coverage Target |
|--------|-----------------|
| Schema validation | 100% of tools |
| Command generation | 100% of PTY-capable tools |
| Output parsing | All tools with fixtures |
| Security checks | All tools with target input |
| Error handling | Common error conditions |

## Troubleshooting

### Common Test Failures

1. **Schema validation fails**
   - Check required fields have defaults or are in minimal params
   - Verify enum values match schema definition

2. **Command contract fails**
   - Implement `build_command()` method
   - Check return type is `List[str]`

3. **Security test fails**
   - Review input sanitization in `build_command()`
   - Check for shell metacharacter handling

4. **Output parsing fails**
   - Verify fixture matches real tool output format
   - Check `parse_output()` handles empty/error output

### Debug Mode

```bash
# Verbose output with print statements
python -m pytest tests/tools/contracts/ -v -s

# Stop on first failure
python -m pytest tests/tools/contracts/ -x

# Show local variables on failure
python -m pytest tests/tools/contracts/ -l
```

## Best Practices

1. **Keep fixtures realistic** - Use actual tool output samples
2. **Test error conditions** - Don't just test happy path
3. **Validate security** - Run injection tests for all input fields
4. **Document patterns** - Add command patterns for new tools
5. **Update on changes** - Keep fixtures in sync with tool updates
