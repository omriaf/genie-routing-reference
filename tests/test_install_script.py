"""One-command installer contract, exercised without workspace access."""

import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def fake_tool(directory: Path, name: str) -> None:
    path = directory / name
    path.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$0 $*\" >> \"$INSTALL_LOG\"\n")
    path.chmod(0o755)


def run_installer(tmp_path: Path, *arguments: str, answer: str = ""):
    tools = tmp_path / "bin"
    tools.mkdir()
    fake_tool(tools, "uv")
    fake_tool(tools, "databricks")
    log = tmp_path / "commands.log"
    env = {
        **os.environ,
        "PATH": f"{tools}:{os.environ['PATH']}",
        "INSTALL_LOG": str(log),
    }
    result = subprocess.run(
        ["bash", str(ROOT / "install.sh"), *arguments], cwd=ROOT, env=env,
        input=answer, text=True, capture_output=True,
    )
    return result, log.read_text().splitlines() if log.exists() else []


def test_installer_requires_explicit_profile(tmp_path):
    result, commands = run_installer(tmp_path)

    assert result.returncode == 2
    assert "--profile" in result.stderr
    assert commands == []


def test_installer_validates_then_stops_when_deployment_is_not_approved(tmp_path):
    result, commands = run_installer(tmp_path, "--profile", "chosen", answer="n\n")

    assert result.returncode == 0
    joined = "\n".join(commands)
    assert "uv sync" in joined
    assert "--frozen" not in joined
    assert "setup.compile_config" in joined
    assert "setup.preflight" in joined
    assert "setup.bundle validate --strict --target customer --profile chosen" in joined
    assert "setup.bootstrap" not in joined
    assert "setup.bundle deploy" not in joined
    assert "Deployment cancelled" in result.stdout


def test_installer_deploys_in_documented_order_after_approval(tmp_path):
    result, commands = run_installer(tmp_path, "--profile", "chosen", "--yes")

    assert result.returncode == 0
    joined = "\n".join(commands)
    expected = (
        "setup.compile_config", "setup.preflight", "setup.bundle validate",
        "setup.bootstrap", "setup.bundle deploy", "router_bootstrap", "router_app",
        "databricks bundle summary --target customer --profile chosen",
    )
    positions = [joined.index(value) for value in expected]
    assert positions == sorted(positions)
    assert "Installation commands completed" in result.stdout
