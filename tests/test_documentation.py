"""Installation contracts: navigation, safety, CLI syntax, and executable examples.

These checks catch broken handoffs, implicit profiles, unsafe credential examples,
and commands that drift from the real offline compiler/preparation interfaces.
"""

import json
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from conftest import model_decision

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = (
    "README.md", "AGENTS.md", "docs/INSTALL_DEMO.md", "docs/INSTALL_CUSTOMER.md",
    "docs/SECURITY.md", "docs/OPERATIONS.md", "docs/AI_ASSISTED_INSTALL.md",
)


def installation_documents():
    pending = set(REQUIRED)
    for file in ROOT.rglob("*.md"):
        relative = file.relative_to(ROOT)
        if any(part.startswith(".") for part in relative.parts):
            continue
        if file.name in {"README.md", "AGENTS.md"} or relative.parts[0] == "docs":
            pending.add(relative.as_posix())
    discovered = set()
    while pending:
        relative = pending.pop()
        if relative in discovered:
            continue
        discovered.add(relative)
        if not (ROOT / relative).is_file():
            continue  # The per-document check reports missing files as failures.
        for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", read(relative)):
            if "://" in target or target.startswith("#"):
                continue
            destination = (ROOT / relative).parent.joinpath(target.split("#")[0]).resolve()
            if destination.suffix == ".md":
                pending.add(destination.relative_to(ROOT).as_posix())
    return tuple(sorted(discovered))


def read(relative):
    file = ROOT / relative
    assert file.is_file(), f"Missing installation document: {relative}"
    return file.read_text(encoding="utf-8")


def commands(document):
    for block in re.findall(r"```(?:bash|sh)\n(.*?)```", document, re.DOTALL):
        for line in block.replace("\\\n", " ").splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                yield shlex.split(line)


@pytest.mark.parametrize("relative", installation_documents())
def test_installation_documents_exist_and_local_links_resolve(relative):
    document = read(relative)
    for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", document):
        if "://" not in target and not target.startswith("#"):
            assert (ROOT / relative).parent.joinpath(target.split("#")[0]).exists(), target


def test_secondary_installation_documents_are_discovered():
    assert {"demo/README.md", "demo/genie/README.md", "customer_config/README.md"} <= set(
        installation_documents()
    )


def test_secondary_installation_broken_links_are_rejected(monkeypatch):
    original_read = read

    def broken_link(relative):
        document = original_read(relative)
        if relative == "demo/genie/README.md":
            document += "\n[Broken handoff](missing-install-guide.md)\n"
        return document

    monkeypatch.setitem(globals(), "read", broken_link)
    with pytest.raises(AssertionError, match="missing-install-guide.md"):
        for relative in installation_documents():
            test_installation_documents_exist_and_local_links_resolve(relative)


@pytest.mark.parametrize("relative, option", [
    ("demo/genie/README.md", "--include-serialized-space"),
    ("docs/OPERATIONS.md", "--target customer"),
])
def test_secondary_or_repeated_cli_example_rejects_unknown_options(monkeypatch, relative, option):
    if shutil.which("databricks") is None:
        pytest.skip("CLI unavailable")
    original_read = read

    def invalid_option(filename):
        document = original_read(filename)
        if filename == relative:
            document = document.replace(option, option + " --documentation-invalid-flag", 1)
        return document

    monkeypatch.setitem(globals(), "read", invalid_option)
    with pytest.raises(AssertionError, match="--documentation-invalid-flag"):
        test_documented_databricks_commands_match_installed_cli_help()


@pytest.mark.parametrize("prefer, expected_route", [(True, "uc_function"), (False, "genie")])
def test_customer_smoke_certified_match_obeys_documented_policy(
    modules, runtime_document, install_runtime, workspace, prefer, expected_route,
):
    document = read("docs/INSTALL_CUSTOMER.md")
    marker = f"`prefer_certified_functions: {str(prefer).lower()}`"
    rows = [line for line in document.splitlines() if line.startswith("|") and marker in line]
    assert len(rows) == 1, f"Missing smoke acceptance for {marker}"
    assert "matched certified" in rows[0].casefold()
    documented_route = re.search(r"route=(\w+)", rows[0])[1]
    assert documented_route == expected_route

    runtime_document["routing"]["prefer_certified_functions"] = prefer
    runtime = install_runtime(runtime_document)
    function = runtime.uc_functions[0]
    workspace.decision = model_decision(domain=function.domain, candidates={function.domain: 0.9})
    enrichment = modules.router.RoutingEnrichment(
        domain=function.domain, certified_function=function.name, method="exact_alias", score=1.0,
    )
    result = modules.agent.ask_analytics("An approved metric", enrichment=enrichment)
    assert result["route"] == documented_route
    assert result["status"] == "answered"
    if prefer:
        assert result["uc_function"] == function.full_name
    else:
        assert result["genie_space_id"] == runtime.agents_by_name[function.domain].space_id
        assert "uc_function" not in result
        assert workspace.calls[-1][0] == "genie"


@pytest.mark.parametrize("prefer", [True, False])
@pytest.mark.parametrize("scenario, route, status", [
    ("no_functions", "genie", "answered"), ("no_match", "genie", "answered"),
    ("undefined_metric", "fail_loud", "undefined_term"),
    ("low_confidence", "fail_loud", "no_route"),
])
def test_customer_smoke_without_function_match_preserves_genie_and_refusal_policy(
    modules, runtime_document, install_runtime, workspace, prefer, scenario, route, status,
):
    document = read("docs/INSTALL_CUSTOMER.md")
    rows = [line for line in document.splitlines() if line.startswith("| No approved function match")]
    assert len(rows) == 1, "Missing no-function/no-match smoke acceptance"
    assert {"genie", "fail_loud"} == set(re.findall(r"route=(\w+)", rows[0]))
    runtime_document["routing"]["prefer_certified_functions"] = prefer
    if scenario == "no_functions":
        runtime_document["uc_functions"] = []
    install_runtime(runtime_document)
    workspace.decision = model_decision()
    if scenario == "undefined_metric":
        workspace.decision["undefined_terms"] = ["Unapproved metric"]
    elif scenario == "low_confidence":
        workspace.decision["confidence"] = 0.1
    result = modules.agent.ask_analytics("A test question without a certified match")
    assert (result["route"], result["status"]) == (route, status)
    if route == "fail_loud":
        assert len(workspace.calls) == 1  # Refused questions never execute a downstream route.


def test_readme_explains_architecture_ownership_and_choice_before_installation():
    document = read("README.md")
    decision = document.index("docs/INSTALL_DEMO.md")
    for required in ("architecture", "you provide", "DAB creates", "security"):
        assert required.casefold() in document[:decision].casefold()
    assert "docs/INSTALL_CUSTOMER.md" in document
    assert "docs/AI_ASSISTED_INSTALL.md" in document


def test_readme_and_ai_handoff_use_the_one_command_installer():
    for relative in ("README.md", "docs/AI_ASSISTED_INSTALL.md"):
        document = read(relative)
        assert "./install.sh --profile \"<PROFILE>\"" in document
    assert "customer_config/customer.yml" in read("README.md")


def test_public_readme_addresses_the_installer_not_an_internal_sender():
    introduction = read("README.md")[:1200].casefold()
    assert "send the customer" not in introduction
    assert "clone" in introduction
    assert "configure" in introduction
    assert "install" in introduction


@pytest.mark.parametrize("relative", ("docs/INSTALL_CUSTOMER.md", "docs/INSTALL_DEMO.md"))
def test_installation_orders_validation_approval_and_mutations(relative):
    document = read(relative)
    steps = list(commands(document))
    joined = [" ".join(step) for step in steps]
    compile_at = next(i for i, cmd in enumerate(joined) if "-m setup.compile_config " in cmd)
    preflight_at = next(i for i, cmd in enumerate(joined) if "-m setup.preflight " in cmd)
    validate_at = next(i for i, cmd in enumerate(joined) if "-m setup.bundle validate --strict" in cmd)
    bootstrap_at = next(i for i, cmd in enumerate(joined) if "-m setup.bootstrap " in cmd)
    deploy_at = next(i for i, cmd in enumerate(joined) if "-m setup.bundle deploy " in cmd)
    assert compile_at < preflight_at < validate_at < bootstrap_at < deploy_at
    assert document.index("deployment approval", document.index("setup.bundle validate")) < document.index("python -m setup.bootstrap")
    for term in ("Python 3.11", "CLI 1.0", "Prerequisites", "smoke", "cleanup", "summary.json"):
        assert term.casefold() in document.casefold()
    if relative.endswith("DEMO.md"):
        for module in ("setup.compile_config", "setup.preflight"):
            assert all("--demo" in cmd for cmd in steps if module in cmd)
        assert "REPLACE_RETAIL_SPACE_ID" in document
        assert "seed.sql" in document
    else:
        assert all("--demo" not in cmd for cmd in steps)
        for term in ("inventory", "fine-grained", "Contents: Read-only", "secret_scope", "secret_key"):
            assert term.casefold() in document.casefold()


@pytest.mark.parametrize("relative", ("AGENTS.md", "docs/AI_ASSISTED_INSTALL.md"))
def test_ai_handoff_contains_public_entry_and_required_safety_gates(relative):
    document = read(relative).casefold()
    for term in ("<public_git_url>", "readme.md", "one at a time", "never request or print",
                 "token values", "never auto-select", "summary.json", "--strict", "approval",
                 "destroy", "customer resources"):
        assert term in document, (relative, term)


def test_every_documented_workspace_command_names_the_profile_and_target():
    for file in sorted(ROOT.rglob("*.md")):
        if any(part.startswith(".") for part in file.relative_to(ROOT).parts):
            continue
        for cmd in commands(file.read_text()):
            workspace_command = cmd[0] == "databricks" or (
                len(cmd) > 2 and cmd[1] == "-m" and cmd[2] in {
                    "setup.preflight", "setup.bootstrap", "setup.bundle",
                }
            )
            if not workspace_command:
                continue
            assert "--profile" in cmd, (file, cmd)
            assert cmd[cmd.index("--profile") + 1] == "<PROFILE>", (file, cmd)
            if "bundle" in cmd or "setup.bundle" in cmd:
                assert "--target" in cmd and cmd[cmd.index("--target") + 1] == "customer"


def test_public_installation_docs_have_no_live_identifiers_or_credential_literals():
    patterns = (
        r"https?://[^\s<>`\"']*(?:azuredatabricks\.net|cloud\.databricks\.com|gcp\.databricks\.com)",
        r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", r"\b[0-9a-fA-F]{32}\b",
        r"\b(?:dapi[0-9a-fA-F]{20,}|github_pat_[A-Za-z0-9_]+|gh[pousr]_[A-Za-z0-9]+)\b",
        r"--(?:string-value|bytes-value)\b", r"\bsecrets\s+get-secret\b",
    )
    for relative in REQUIRED:
        document = read(relative)
        for pattern in patterns:
            assert not re.search(pattern, document), (relative, pattern)


def test_operations_documents_cleanup_and_security_explains_data_retention():
    operations = read("docs/OPERATIONS.md").casefold()
    for term in ("update", "troubleshooting", "cleanup", "bundle destroy", "routing_traces", "approval"):
        assert term in operations
    security = read("docs/SECURITY.md").casefold()
    for term in ("read-only", "secret", "question", "answer", "retention", "app principal", "delegation"):
        assert term in security


@pytest.mark.parametrize("relative", ("docs/INSTALL_CUSTOMER.md", "docs/INSTALL_DEMO.md"))
def test_smoke_instructions_allow_delta_initial_state_then_verify_post_request_health(relative):
    document = read(relative)
    smoke = document[document.casefold().index("## 7. customer smoke" if "CUSTOMER" in relative
                                               else "## 6. demo smoke"):]
    assert "not_ready" in smoke
    assert "first request" in smoke
    assert "both trace sinks" in smoke


def test_documented_markdown_example_is_accepted_by_real_parser():
    from app.markdown_definitions import parse_definition

    document = read("docs/INSTALL_CUSTOMER.md")
    example = re.search(r"```yaml\n(.*?)```", document, re.DOTALL)[1]
    for placeholder, value in {
        "<METRIC_KEY>": "approved_metric", "<DOMAIN_NAME>": "approved_domain",
        "<BUSINESS_PHRASE>": "approved business phrase",
        "<FUNCTION_LOGICAL_NAME>": "approved_function",
    }.items():
        example = example.replace(placeholder, value)
    parsed = parse_definition(example, "definitions/approved_metric.md")
    assert (parsed.name, parsed.domain, parsed.certified_function) == (
        "approved_metric", "approved_domain", "approved_function",
    )


def run_python_command(cmd, cwd):
    result = subprocess.run([sys.executable, *cmd[1:]], cwd=cwd, text=True,
                            capture_output=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    return result


@pytest.mark.parametrize("relative", ("docs/INSTALL_CUSTOMER.md", "docs/INSTALL_DEMO.md"))
def test_documented_offline_install_commands_produce_deployable_artifacts(relative, tmp_path):
    # Run from a real isolated source checkout, as the guides require. The demo
    # is a source tree rather than an installed Python distribution package.
    for directory in ("setup", "app", "demo", "resources", "sql", "dashboards"):
        shutil.copytree(ROOT / directory, tmp_path / directory,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    steps = list(commands(read(relative)))
    if relative.endswith("DEMO.md"):
        prepare = next(cmd for cmd in steps if "demo.generators.prepare" in cmd)
        substitutions = {
            "<CATALOG>": "documentation_catalog", "<DEMO_SCHEMA>": "documentation_demo",
            "<WAREHOUSE_ID>": "offlinewarehouse", "<CLASSIFIER_ENDPOINT>": "offline-classifier",
            "<EXPERIMENT_PATH>": "/Shared/documentation-router",
            "<DEFINITIONS_REPOSITORY_URL>": "https://github.com/example/definitions",
            "<DEFINITIONS_BRANCH>": "main", "<SECRET_SCOPE>": "offline-scope",
            "<SECRET_KEY>": "offline-key", "<GENIE_PARENT_PATH>": "/Workspace/Shared/documentation",
        }
        run_python_command([substitutions.get(arg, arg) for arg in prepare], tmp_path)
        config_file = tmp_path / ".generated-demo-setup/demo.yml"
    else:
        config_file = tmp_path / "customer_config/customer.yml"
        config_file.parent.mkdir()
        shutil.copyfile(ROOT / "customer_config/customer.example.yml", config_file)
    config = yaml.safe_load(config_file.read_text())
    for index, agent in enumerate(config["genie_agents"], 1):
        agent["space_id"] = f"{index:032x}"  # Explicitly synthetic, never a live lookup.
    config_file.write_text(yaml.safe_dump(config))
    compile_cmd = next(cmd for cmd in steps if "setup.compile_config" in cmd)
    run_python_command(compile_cmd, tmp_path)
    generated = tmp_path / ".generated"
    summary_cmd = next(cmd for cmd in steps if "json.tool" in cmd and ".generated/summary.json" in cmd)
    summary = json.loads(run_python_command(summary_cmd, tmp_path).stdout)
    assert summary["demo"] == relative.endswith("DEMO.md")
    assert summary["resource_counts"]["genie_spaces"] == len(config["genie_agents"])
    assert "space_id" not in json.dumps(summary)
    assert (generated / "app_source/runtime_config.json").is_file()
    resources = yaml.safe_load((generated / "customer.resources.yml").read_text())["resources"]
    assert ("demo_seed" in resources["jobs"]) == summary["demo"]
    resource_keys = {key for kind in resources.values() for key in kind}
    for cmd in steps:
        if "--resource" in cmd:
            assert cmd[cmd.index("--resource") + 1] in resource_keys


def test_documented_databricks_commands_match_installed_cli_help():
    executable = shutil.which("databricks")
    if executable is None:
        pytest.skip("CLI not installed; no workspace or credentials required by this check")
    help_text = {}
    for relative in installation_documents():
        for cmd in commands(read(relative)):
            if cmd[0] != "databricks":
                continue
            command_path = []
            for word in cmd[1:]:
                if word.startswith(("-", "<", "@")):
                    break
                command_path.append(word)
            key = tuple(command_path)
            if key not in help_text:
                result = subprocess.run([executable, *command_path, "--help", "--profile", "<PROFILE>"],
                                        cwd=ROOT, capture_output=True, text=True, timeout=15, check=False)
                assert result.returncode == 0, (relative, key, result.stderr)
                help_text[key] = result.stdout
            # Catch unknown options even when help itself succeeds.
            for option in (arg for arg in cmd if arg.startswith("--")):
                assert option in help_text[key], (relative, key, option)
