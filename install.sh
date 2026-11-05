#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: ./install.sh --profile <DATABRICKS_PROFILE> [--config <PATH>] [--yes]" >&2
}

selected_cli_profile=""
config="customer_config/customer.yml"
approved="false"
while (($#)); do
  case "$1" in
    --profile)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      selected_cli_profile="$2"
      shift 2
      ;;
    --config)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      config="$2"
      shift 2
      ;;
    --yes)
      approved="true"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage
      exit 2
      ;;
  esac
done

if [[ -z "$selected_cli_profile" || ! "$selected_cli_profile" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]; then
  echo "A valid explicit --profile is required." >&2
  usage
  exit 2
fi

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$root"

command -v uv >/dev/null || { echo "uv is required: https://docs.astral.sh/uv/" >&2; exit 1; }
command -v databricks >/dev/null || { echo "Databricks CLI 1.0+ is required." >&2; exit 1; }
[[ -f "$config" ]] || {
  echo "Missing $config. Copy customer_config/customer.example.yml and replace its placeholders." >&2
  exit 1
}

echo "Preparing dependencies and validating the installation for profile: $selected_cli_profile"
uv sync
uv run --no-sync python -m setup.compile_config --config "$config" --output .generated
uv run --no-sync python -m setup.preflight --config "$config" --output .generated --profile "$selected_cli_profile"
uv run --no-sync python -m setup.bundle validate --strict --target customer --profile "$selected_cli_profile"

echo "Redacted installation summary:"
uv run --no-sync python -m json.tool .generated/summary.json
echo "Target: customer"
echo "Profile: $selected_cli_profile"

if [[ "$approved" != "true" ]]; then
  read -r -p "Deploy and bootstrap these resources now? [y/N] " answer
  if [[ ! "$answer" =~ ^[Yy]$ ]]; then
    echo "Deployment cancelled; validation completed successfully."
    exit 0
  fi
fi

uv run --no-sync python -m setup.bootstrap --config .generated/runtime_config.json --profile "$selected_cli_profile"
uv run --no-sync python -m setup.bundle deploy --target customer --profile "$selected_cli_profile"
uv run --no-sync python -m setup.bundle run --resource router_bootstrap --target customer --profile "$selected_cli_profile"
uv run --no-sync python -m setup.bundle run --resource router_app --target customer --profile "$selected_cli_profile"
databricks bundle summary --target customer --profile "$selected_cli_profile"

echo "Installation commands completed. Follow docs/INSTALL_CUSTOMER.md for live smoke tests."
