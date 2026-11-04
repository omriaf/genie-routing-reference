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
