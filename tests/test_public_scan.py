"""Publication gates exercised against disposable Git repositories, offline."""

from pathlib import Path
import subprocess
import sys

import pytest

from scripts import scan_public_repo as scan


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def repository(tmp_path):
    git(tmp_path, "init", "--initial-branch=main")
    git(tmp_path, "config", "user.email", "analyst@example.com")
    git(tmp_path, "config", "user.name", "Synthetic Author")
    git(tmp_path, "config", "core.hooksPath", "/dev/null")
    (tmp_path / "README.md").write_text("Public synthetic project\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "Initial source")
    return tmp_path


def secret():
    # Construct negative-test payloads so no token-shaped literal is published.
    return "da" + "pi" + "a1" * 16


@pytest.mark.parametrize("payload,rule", [
    (lambda: "person@" + "private.test", "email"),
    (lambda: "/" + "Users/person/work", "home-path"),
    (lambda: "https://sample." + "cloud.databricks.com", "workspace-host"),
    (lambda: "https://sample." + "azuredatabricks.net", "workspace-host"),
    (lambda: "https://sample.aws." + "databricksapps.com", "workspace-host"),
    (lambda: "workspace_id: " + "123456789", "resource-id"),
    (lambda: '{"workspace_id": ' + '123456789}', "resource-id"),
    (lambda: 'space_id: "' + "12ab" * 8 + '"', "resource-id"),
    (lambda: "databricks apps list --profile " + "private-profile", "profile-literal"),
    (lambda: 'profile = "' + "private-profile" + '"', "profile-literal"),
    (lambda: '["--profile", "' + 'private-profile"]', "profile-literal"),
    (secret, "credential"),
    (lambda: "gh" + "p_" + "a" * 36, "credential"),
    (lambda: "github_" + "pat_" + "a" * 40, "credential"),
    (lambda: "AK" + "IA" + "A" * 16, "credential"),
    (lambda: "-----BEGIN " + "RSA PRIVATE KEY-----", "private-key"),
    (lambda: 'password: "' + "private-value" + '"', "secret-literal"),
    (lambda: "Assistant" + ": internal discussion", "internal-source"),
    (lambda: "<" + "|im_start|>", "internal-source"),
])
def test_detection_rules(payload, rule):
    findings = scan.scan_text(payload(), "synthetic")
    assert rule in {finding.rule for finding in findings}
    assert payload() not in repr(findings)


def test_documented_placeholders_are_narrow():
    assert not scan.scan_text(
        'analyst@example.com\nwarehouse_id: abc123\n'
        'space_id: "01abc000000000000000000000000001"\n'
        'databricks apps list --profile "<PROFILE>"\n', "synthetic",
    )
    assert scan.scan_text('warehouse_id: "' + 'abc1234"', "synthetic")
    assert scan.scan_text("analyst@example.com " + secret(), "synthetic")


def test_clean_repo_and_ignored_private_configuration(repository):
    (repository / ".gitignore").write_text("validation/local/\n")
    git(repository, "add", ".gitignore")
    local = repository / "validation/local"
    local.mkdir(parents=True)
    (local / "smoke.json").write_text(secret())
    findings, counts = scan.scan_repository(repository)
    assert findings == []
    assert counts["tracked_files"] == 2
    assert counts["history_commits"] == 1


def test_worktree_and_index_are_both_scanned(repository):
    path = repository / "README.md"
    path.write_text(secret())
    assert any(f.location.startswith("tracked:") for f in scan.scan_repository(repository)[0])
    git(repository, "add", "README.md")
    path.write_text("Clean worktree\n")
    assert any(f.location.startswith("index:") for f in scan.scan_repository(repository)[0])


def test_deleted_content_on_other_branch_is_still_scanned(repository):
    git(repository, "checkout", "-b", "historical")
    path = repository / "removed.txt"
    path.write_text(secret())
    git(repository, "add", ".")
    git(repository, "commit", "-m", "Historical source")
    git(repository, "rm", "removed.txt")
    git(repository, "commit", "-m", "Remove source")
    git(repository, "checkout", "main")
    findings, counts = scan.scan_repository(repository)
    assert counts["history_commits"] == 3
    assert any(f.rule == "credential" and f.location.startswith("blob:") for f in findings)


@pytest.mark.parametrize("path", [
    ".databrickscfg", "customer_config/customer.yml", "validation/local/smoke.json",
    "validation/internal-test-results.md", ".generated/runtime_config.json",
    ".env.production", "notes/transcript.md", "id_ed25519", "key.pem",
])
def test_private_paths_fail_even_with_empty_content(repository, path):
    destination = repository / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("")
    git(repository, "add", path)
    assert any(f.rule == "private-path" for f in scan.scan_repository(repository)[0])


def test_commit_and_tag_metadata_are_scanned(repository):
    git(repository, "commit", "--allow-empty", "-m", secret())
    git(repository, "tag", "-a", "release", "-m", secret())
    findings, _ = scan.scan_repository(repository)
    assert any(f.location.startswith("commit:") and f.rule == "credential" for f in findings)
    assert any(f.location.startswith("tag:") and f.rule == "credential" for f in findings)


def test_non_placeholder_author_is_rejected(repository):
    git(repository, "-c", "user.email=person@" + "private.test", "commit", "--allow-empty", "-m", "Synthetic")
    assert any(f.location.startswith("commit:") and f.rule == "email"
               for f in scan.scan_repository(repository)[0])


def test_symlinks_and_binary_are_fail_closed(repository, tmp_path):
    outside = tmp_path / "outside"
    outside.write_text(secret())
    link = repository / "link"
    link.symlink_to(outside)
    git(repository, "add", "link")
    assert any(f.rule == "unsupported-index-entry" for f in scan.scan_repository(repository)[0])
    git(repository, "rm", "--cached", "link")
    link.unlink()
    (repository / "blob.bin").write_bytes(b"\x00\xff")
    git(repository, "add", "blob.bin")
    assert any(f.rule == "unreviewed-binary" for f in scan.scan_repository(repository)[0])


def test_shallow_clone_is_not_publication_evidence(repository, tmp_path_factory):
    clone = tmp_path_factory.mktemp("shallow") / "clone"
    git(repository, "clone", "--depth=1", repository.as_uri(), str(clone))
    with pytest.raises(ValueError, match="history"):
        scan.scan_repository(clone)


def test_cli_redacts_matches_and_private_filenames(repository):
    filename = "person@" + "private.test"
    (repository / filename).write_text(secret())
    git(repository, "add", filename)
    result = subprocess.run([sys.executable, str(Path(scan.__file__)), "--root", str(repository)],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 1
    assert "rule=credential" in result.stdout
    assert filename not in result.stdout + result.stderr
    assert secret() not in result.stdout + result.stderr


def test_private_deny_values_override_placeholders_and_cover_history(repository):
    assert scan.scan_text("public reference abc123", "synthetic", ("ABC123",))[0].rule == "private-deny-value"
    git(repository, "commit", "--allow-empty", "-m", "Unique customer reference")
    findings, _ = scan.scan_repository(repository, ("unique customer",))
    assert any(f.rule == "private-deny-value" and f.location.startswith("commit:") for f in findings)


def test_allowlist_values_have_documented_reasons():
    documentation = (Path(__file__).resolve().parents[1] / "validation/README.md").read_text()
    for values in scan.SYNTHETIC.values():
        assert all(f"`{value}`" in documentation for value in values)


ASSIGNMENT_CASES = (
    ("profile", "profile-literal"),
    ("DATABRICKS_CONFIG_PROFILE", "profile-literal"),
    ("password", "secret-literal"),
    ("DATABRICKS_CLIENT_SECRET", "secret-literal"),
    ("ARM_CLIENT_SECRET", "secret-literal"),
    ("api_key", "secret-literal"),
    ("access_token", "secret-literal"),
    ("refresh_token", "secret-literal"),
)


@pytest.mark.parametrize("key,rule", ASSIGNMENT_CASES)
@pytest.mark.parametrize("template", (
    "{key}: {value}", "{key}={value}", "export {key}={value}",
    '{{"{key}": {value}}}', '{key} = {value} # private setting',
    "'{key}' = '{value}'", '"{key}" = "{value}"',
    '{{"safe": 1, "{key}": {value}}}', "{key}={value} command", "{key}={value}; command",
))
def test_private_assignments_are_detected_and_redacted(key, rule, template):
    value = "private-" + "assignment-sentinel"
    payload = template.format(key=key, value=value)
    findings = scan.scan_text(payload, "synthetic")
    assert rule in {finding.rule for finding in findings}
    assert value not in repr(findings)


@pytest.mark.parametrize("payload", (
    "profile: <PROFILE>", "DATABRICKS_CONFIG_PROFILE=<PROFILE>",
    "password: <PASSWORD>", "export DATABRICKS_CLIENT_SECRET=<CLIENT_SECRET>",
    '{"password": "<PASSWORD>"}', "api_" + 'key = "<API_KEY>"',
    "access_token: PLACEHOLDER", "refresh_token: <TOKEN>",
    "password: null", "password = None", "profile: ~",
))
def test_explicit_unconfigured_assignment_placeholders_are_allowed(payload):
    assert scan.scan_text(payload, "synthetic") == []


@pytest.mark.parametrize("key,value", (
    ("profile", "<PROFILE>-private"), ("password", "<PASSWORD>-private"),
    ("password", "<UNREVIEWED_PLACEHOLDER>"), ("password", '"null"'),
    ("password", '"None"'), ("profile", '"~"'),
))
def test_nearby_and_undocumented_assignment_values_are_not_exempted(key, value):
    assert scan.scan_text(key + ": " + value, "synthetic")


@pytest.mark.parametrize("key,placeholder", (("profile", "<PROFILE>"), ("password", "<PASSWORD>")))
def test_shell_concatenation_cannot_launder_placeholders(key, placeholder):
    assert scan.scan_text(key + '="' + placeholder + '"private', "synthetic")


def test_python_expressions_and_type_annotations_are_not_private_literals():
    assert scan.scan_text(
        'def create(profile: str):\n'
        '    return WorkspaceClient(profile=profile)\n'
        'token = os.environ.get("SAFE_TOKEN_REFERENCE")\n', "synthetic",
    ) == []


@pytest.mark.parametrize("key,rule", ASSIGNMENT_CASES)
def test_unquoted_assignments_fail_in_worktree_index_and_deleted_history(repository, key, rule, capsys):
    value = "private-" + "assignment-sentinel"
    path = repository / "settings.txt"
    path.write_text(key + "=" + value + "\n")
    git(repository, "add", "settings.txt")
    findings, _ = scan.scan_repository(repository)
    assert any(f.rule == rule and f.location.startswith("tracked:") for f in findings)
    assert any(f.rule == rule and f.location.startswith("index:") for f in findings)
    git(repository, "commit", "-m", "Temporary settings")
    git(repository, "rm", "settings.txt")
    git(repository, "commit", "-m", "Remove temporary settings")
    findings, _ = scan.scan_repository(repository)
    assert any(f.rule == rule and f.location.startswith("blob:") for f in findings)
    assert scan.main(["--root", str(repository)]) == 1
    output = capsys.readouterr()
    assert value not in output.out + output.err


LEADING_CONTEXT_MUTATIONS = (
    "- {key}: {value}", "    - {key}: {value}", "  - - {key}: {value}",
    "    {key}: {value}", "    {key}={value}", "export {key}={value}",
    "env {key}={value} command", "/usr/bin/env {key}={value} command",
    "command env {key}={value} command", "sudo -u sample env {key}={value} command",
    "env -u SAFE_SETTING {key}={value} command", "env -- {key}={value} command",
    "true; export {key}={value}", "true;export {key}={value}",
    "true && export {key}={value}", "true&&env {key}={value} command",
    "false || export {key}={value}", "false||env {key}={value} command",
    "true | env {key}={value} command", "true & env {key}={value} command",
    "( env {key}={value} command )", "SAFE_SETTING=value {key}={value} command",
    "env SAFE_SETTING=value {key}={value} SECOND_SETTING=value command",
    "true; SAFE_SETTING=value {key}={value} command",
    "export SAFE_SETTING=value {key}={value}; command",
    "{{ env {key}={value} command; }}", "if true; then export {key}={value}; fi",
    "while {key}={value} command; do true; done", "until {key}={value} command; do true; done",
    "if true; then true; else env {key}={value} command; fi", "! env {key}={value} command",
    "command -p env {key}={value} command", "exec -a sample env {key}={value} command",
    "readonly SAFE_SETTING {key}={value}", "declare -x {key}={value}",
    "env SAFE_SETTING=value \\\n    {key}={value} command",
    '{{safe: value, {key}: {value}}}', '[{{"{key}": {value}}}]',
    '[credentials]\n{key} = {value}',
)


@pytest.mark.parametrize("key,rule", (
    ("profile", "profile-literal"), ("password", "secret-literal"),
    ("DATABRICKS_CLIENT_SECRET", "secret-literal"),
))
@pytest.mark.parametrize("template", LEADING_CONTEXT_MUTATIONS)
def test_assignment_leading_context_mutations(template, key, rule):
    value = "review-synthetic-" + "private-value"
    findings = scan.scan_text(template.format(key=key, value=value), "synthetic")
    assert rule in {finding.rule for finding in findings}
    assert value not in repr(findings)


@pytest.mark.parametrize("key,value", (("profile", "<PROFILE>"), ("password", "<PASSWORD>")))
@pytest.mark.parametrize("template", LEADING_CONTEXT_MUTATIONS)
def test_leading_contexts_preserve_only_exact_placeholders(template, key, value):
    assert not scan.scan_text(template.format(key=key, value=value), "synthetic")
    assert scan.scan_text(template.format(key=key, value=value + "-private"), "synthetic")


@pytest.mark.parametrize("prose", (
    "The profile: setting is discussed in this sentence.",
    "Documentation describes password: value as a phrase.",
    "Use password=value when describing the syntax.",
    "The phrase env DATABRICKS_CLIENT_SECRET=value is a prose example.",
    "A bullet describes - profile: value within prose.",
    "echo password=value", "echo 'env password=value'",
))
def test_assignment_words_in_prose_are_not_indiscriminately_flagged(prose):
    assert not scan.scan_text(prose, "synthetic")


@pytest.mark.parametrize("key,placeholder", (("profile", "<PROFILE>"), ("password", "<PASSWORD>")))
def test_yaml_placeholder_must_cover_the_entire_plain_scalar(key, placeholder):
    for template in ("- {key}: {value}", "{{safe: value, {key}: {value}}}"):
        assert not scan.scan_text(template.format(key=key, value=placeholder + " # comment"), "synthetic")
        assert scan.scan_text(template.format(key=key, value=placeholder + " private-suffix"), "synthetic")


@pytest.mark.parametrize("prefix", ("", "env ", "true; export "))
def test_shell_continuations_cannot_launder_placeholder_prefixes(prefix):
    value = "PLACEHOLDER" + "\\\nprivate-suffix"
    findings = scan.scan_text(prefix + "pass" + "word=" + value + " command", "synthetic")
    assert "secret-literal" in {finding.rule for finding in findings}
    assert value not in repr(findings)


def test_every_leading_context_is_scanned_in_index_worktree_and_deleted_blobs(repository, capsys):
    value = "review-synthetic-" + "private-value"
    rows = [(template.format(key=key, value=value), rule)
            for template in LEADING_CONTEXT_MUTATIONS
            for key, rule in (("profile", "profile-literal"), ("password", "secret-literal"),
                              ("DATABRICKS_CLIENT_SECRET", "secret-literal"))]
    contents = "\n".join(row for row, _ in rows) + "\n"
    expected = set()
    line = 1
    for row, rule in rows:
        expected.add((rule, line + (1 if "\\\n" in row or row.startswith("[credentials]") else 0)))
        line += row.count("\n") + 1
    # Expected rules/physical line numbers do not come from the scanner itself.
    assert len(expected) == len(rows)
    path = repository / "settings.txt"
    path.write_text(contents)
    git(repository, "add", "settings.txt")
    findings, _ = scan.scan_repository(repository)
    for boundary in ("tracked:", "index:"):
        assert expected <= {(item.rule, item.line) for item in findings if item.location.startswith(boundary)}
    git(repository, "commit", "-m", "Temporary mutation settings")
    git(repository, "rm", "settings.txt")
    git(repository, "commit", "-m", "Remove mutation settings")
    findings, _ = scan.scan_repository(repository)
    assert expected <= {(item.rule, item.line) for item in findings if item.location.startswith("blob:")}
    assert scan.main(["--root", str(repository)]) == 1
    output = capsys.readouterr()
    assert value not in output.out + output.err
