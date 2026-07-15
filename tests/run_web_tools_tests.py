import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def build_pytest_args(args: argparse.Namespace) -> list[str]:
    pytest_args: list[str] = []
    if args.coverage:
        pytest_args.extend(
            [
                "--cov=agent.tools.web_applications",
                "--cov=agent/tests/tools/web_applications",
                "--cov-report=term-missing",
            ]
        )

    targets: list[str] = []
    if args.unit_only:
        targets.append("agent/tests/tools/web_applications")
    elif args.integration_only:
        targets.append("tests/integration/test_web_tools_integration.py")
        targets.append("tests/integration/test_web_tools_parsing_integration.py")
    elif args.e2e_only:
        targets.append("tests/e2e/test_web_tools_kali.py")
    else:
        targets.extend(
            [
                "agent/tests/tools/web_applications",
                "tests/integration/test_web_tools_integration.py",
                "tests/integration/test_web_tools_parsing_integration.py",
                "tests/e2e/test_web_tools_kali.py",
            ]
        )

    pytest_args.extend(targets)

    if args.skip_slow:
        pytest_args.extend(["-m", "not slow"])

    return pytest_args


def main():
    parser = argparse.ArgumentParser(description="Run web application tool tests")
    parser.add_argument("--unit-only", action="store_true", help="Run only unit tests")
    parser.add_argument("--integration-only", action="store_true", help="Run only integration tests")
    parser.add_argument("--e2e-only", action="store_true", help="Run only E2E tests")
    parser.add_argument("--skip-slow", action="store_true", help="Skip tests marked as slow")
    parser.add_argument("--coverage", action="store_true", help="Generate coverage report")
    parsed = parser.parse_args()

    pytest_cmd = ["pytest"] + build_pytest_args(parsed)
    print("Running:", " ".join(pytest_cmd))
    result = subprocess.run(pytest_cmd, cwd=ROOT)
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()


