"""Bundle entrypoint requiring a user-selected --profile for every operation.

Examples: python -m setup.bundle validate --strict --profile NAME
          python -m setup.bundle deploy --profile NAME
          python -m setup.bundle run --resource router_bootstrap --profile NAME

The upstream CLI can use ambient credentials; YAML cannot require a CLI flag.
Use this entrypoint to enforce the package's explicit-profile contract.
"""

import subprocess
import sys
from pathlib import Path

from setup.compile_config import _ArgumentParser


def main(argv: list[str] | None = None) -> int:
    parser = _ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("operation", choices=("validate", "deploy", "run"))
    parser.add_argument("--profile", required=True)
    parser.add_argument("--target", choices=("customer",), default="customer")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--resource")
    args = parser.parse_args(argv)
    if (not args.profile.strip() or (args.strict and args.operation != "validate")
            or (bool(args.resource) != (args.operation == "run"))):
        parser.error("Invalid bundle arguments")
    command = ["databricks", "bundle", args.operation]
    if args.resource:
        command.append(args.resource)
    if args.strict:
        command.append("--strict")
    command.extend(["--target", args.target, "--profile", args.profile])
    try:
        return subprocess.run(command, cwd=Path(__file__).resolve().parents[1], check=False).returncode
    except OSError:
        print("Bundle CLI execution failed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
