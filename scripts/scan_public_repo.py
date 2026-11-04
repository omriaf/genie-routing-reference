"""Offline publication gate: scan the worktree, index and all reachable history.

Diagnostics contain rule names, object IDs and line numbers, never source text
or filenames (which can themselves contain private values). No network or auth.
The narrow synthetic exceptions are documented in validation/README.md.
"""

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
import sys


_SECRET_KEYS = (
    r"(?:DATABRICKS_|GITHUB_|ARM_|AZURE_)?(?:TOKEN|ACCESS_TOKEN|REFRESH_TOKEN|CLIENT_SECRET|"
    r"PASSWORD|API_KEY|SECRET_VALUE)|GITHUB_DEFINITIONS_TOKEN"
)
_PROFILE_KEYS = r"profile|DATABRICKS_CONFIG_PROFILE"


def _assignments(keys: str) -> str:
    # Quoted literals can appear in source as well as configuration. Bare values
    # need configuration/shell boundaries so Python arguments/type annotations
    # are not mistaken for private literal settings. Match inline JSON keys too.
    key = (r"(?:\"(?:" + keys + r")\"|'(?:" + keys + r")'|(?:" + keys
           + r"))[ \t]*[:=][ \t]*")
    return (
        r"(?<![\w.])" + key + r"[\"']([^\"'\r\n]+)[\"']|"
        r"(?m:(?:^[ \t]*(?:export[ \t]+)?|[{][ \t]*|,[ \t]*(?=[\"']))"
        + key + r"(?P<bare_value>[^\s\"'`,;#{}()[\]\\]+)(?=$|[ \t,}#;\r\n]))"
    )


RULES = {
    "email": re.compile(r"[A-Za-z0-9_.+%-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "home-path": re.compile(r"/(?:Users|home)/[^\s\"'`<>]+"),
    "workspace-host": re.compile(
        r"\b[A-Za-z0-9_.-]+\.(?:cloud\.databricks\.com|azuredatabricks\.net|"
        r"gcp\.databricks\.com|(?:[a-z]+\.)?databricksapps\.com)\b", re.I,
    ),
    "internal-host": re.compile(r"\b[A-Za-z0-9_.-]+\.(?:corp|internal|local)\b", re.I),
    "internal-source": re.compile(
        r"one[-]env|dog[f]ood|log[f]ood|"
        r"<\|(?:im_start|im_end|system|assistant|user)\|>|"
        r"(?m:^\s*(?:Human|Assistant):)|BEGIN (?:INTERNAL|PRIVATE) TRANSCRIPT", re.I,
    ),
    "credential": re.compile(
        r"\bdapi[a-f0-9]{16,}\b|\bgh[pousr]_[A-Za-z0-9]{20,}\b|"
        r"\bgithub_pat_[A-Za-z0-9_]{20,}\b|\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|"
        r"\bxox[baprs]-[A-Za-z0-9-]{10,}|"
        r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
    ),
    "private-key": re.compile(r"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----"),
    "secret-literal": re.compile(_assignments(_SECRET_KEYS), re.IGNORECASE),
    "profile-literal": re.compile(
        r"(?m:^(?:\s*|Examples: )(?:python|databricks)[^\n]*?--profile[ =]+[\"']?([A-Za-z0-9_.-]+))|"
        r"--profile[\"']\s*,\s*[\"']([A-Za-z0-9_<][^\"'\n]*)[\"']|"
        + _assignments(_PROFILE_KEYS), re.IGNORECASE,
    ),
    "resource-id": re.compile(
        r"(?i:[\"']?(?:workspace_id|warehouse_id|space_id|account_id)[\"']?)\s*[:=]\s*"
        r"[\"']([A-Za-z0-9_-]+)[\"']|"
        r"(?m:^\s*(?:workspace_id|warehouse_id|space_id|account_id):\s*([A-Za-z0-9_-]+)\s*$)|"
        r"(?i:[\"']?(?:workspace_id|warehouse_id|space_id|account_id)[\"']?)\s*[:=]\s*(\d{4,})\b",
    ),
    "workspace-id": re.compile(r"[?&]o=\d+\b"),
}

# Exact values only; never allow a whole file, line, directory, or secret rule.
SYNTHETIC = {
    "profile-literal": {"chosen", "selected", "NAME", "NAME.", "test-profile", "sync-test",
                        "<PROFILE>", "<your-selected-profile>"},
    "secret-literal": {"TOKEN", "PLACEHOLDER", "REJECTED_TEST_PLACEHOLDER",
                       "<PASSWORD>", "<CLIENT_SECRET>", "<API_KEY>", "<TOKEN>",
                       "synthetic-secret-in-backend-error", "ENVIRONMENT_CONTENT_MUST_STAY_PRIVATE",
                       "synthetic-bound-token-DO-NOT-LOG"},
    "resource-id": {"abc123", "987abc", "warehouse123", "offlinewarehouse", "space-123", "not", "has", "f",
                    "01abc", "g", "0", "REPLACE_RETAIL_SPACE_ID",
                    "REPLACE_SUPPORT_SPACE_ID", "REPLACE_MARKETING_SPACE_ID",
                    "01abc000000000000000000000000001", "01abc000000000000000000000000002"},
}
PRIVATE_PARTS = {".databrickscfg", ".databricks", ".aws", ".ssh", ".env", ".superpowers",
                 ".claude", ".codex", "node_modules", ".venv", "__pycache__"}


@dataclass(frozen=True)
class Finding:
    location: str
    rule: str
    line: int = 0


def scan_text(contents: str, location: str, denied: tuple[str, ...] = ()) -> list[Finding]:
    findings = []
    for value in denied:
        if value.casefold() in contents.casefold():
            findings.append(Finding(location, "private-deny-value"))
    for rule, pattern in RULES.items():
        for match in pattern.finditer(contents):
            value = next((group for group in match.groups() if group is not None), match[0])
            if match.groupdict().get("bare_value") in {"null", "None", "~"}:
                continue  # Explicit unconfigured/null markers, never quoted literals.
            if rule == "email" and (
                value.rsplit("@", 1)[1].lower() in {"example.com", "example.org", "example.net"}
                or value in {"omriaf@users.noreply.github.com", "placeholder@github.com",
                             "password@github.com", "git@github.com", "secret@api.github.com",
                             "github.com@evil.test"}
            ):
                continue
            if value in SYNTHETIC.get(rule, set()):
                # A quoted placeholder followed by shell concatenation is a
                # different value, not an allowed placeholder.
                if rule not in {"secret-literal", "profile-literal"} or (
                    match.end() == len(contents) or contents[match.end()] in " \t\r\n,]}):;#"
                ):
                    continue
            findings.append(Finding(location, rule, contents.count("\n", 0, match.start()) + 1))
    return findings


def scan_path(path: str, location: str, denied: tuple[str, ...] = ()) -> list[Finding]:
    parts = Path(path).parts
    private = (
        any(part.lower() in PRIVATE_PARTS or part.lower().startswith(".env.") for part in parts)
        or path == "customer_config/customer.yml"
        or path.startswith((".generated", "validation/local/"))
        or path == "validation/internal-test-results.md"
        or any(re.search(r"(?i)(?:transcript|internal[-_]test[-_]results|id_rsa|id_ed25519)", part)
               for part in parts)
        or Path(path).suffix.lower() in {".pem", ".key", ".p12", ".pfx", ".tfstate"}
    )
    return ([Finding(location, "private-path")] if private else []) + scan_text(path, location, denied)


def git(root: Path, *args: str, data: bytes | None = None) -> bytes:
    result = subprocess.run(["git", "-C", str(root), *args], input=data,
                            capture_output=True, check=False, timeout=120)
    if result.returncode:
        raise ValueError("Git scan failed")
    return result.stdout


def scan_repository(root: Path, denied: tuple[str, ...] = ()) -> tuple[list[Finding], dict[str, int]]:
    root = root.resolve()
    # An incomplete clone must never produce publication-ready evidence.
    if git(root, "rev-parse", "--show-toplevel").decode().strip() != str(root):
        raise ValueError("Use the repository root")
    if git(root, "rev-parse", "--is-shallow-repository").strip() != b"false":
        raise ValueError("Full local history is required")
    grafts_path = Path(os.fsdecode(git(root, "rev-parse", "--git-path", "info/grafts")).strip())
    if git(root, "replace", "-l").strip() or (root / grafts_path).exists():
        raise ValueError("History replacement is unsupported")
    findings, blobs = [], set()
    counts = {"tracked_files": 0, "history_commits": 0, "unique_blobs": 0}

    def blob(oid, location):
        if oid in blobs:
            return
        blobs.add(oid)
        contents = git(root, "cat-file", "blob", oid)
        try:
            decoded = contents.decode("utf-8")
            if "\x00" in decoded:
                raise UnicodeError
        except UnicodeError:
            findings.append(Finding(location, "unreviewed-binary"))
        else:
            findings.extend(scan_text(decoded, location, denied))

    for index, entry in enumerate(git(root, "ls-files", "--stage", "-z").split(b"\0")):
        if not entry:
            continue
        metadata, raw_path = entry.split(b"\t", 1)
        mode, oid, stage = metadata.decode().split()
        path = os.fsdecode(raw_path)
        location = f"tracked:{index + 1}"
        counts["tracked_files"] += 1
        findings.extend(scan_path(path, location, denied))
        if stage != "0" or mode not in {"100644", "100755"}:
            findings.append(Finding(location, "unsupported-index-entry"))
            continue
        blob(oid, f"index:{oid}")
        file = root / path
        if file.is_symlink() or any(parent.is_symlink() for parent in file.parents if parent != root):
            findings.append(Finding(location, "worktree-symlink"))
            continue
        try:
            contents = file.read_bytes().decode("utf-8")
            if "\x00" in contents:
                raise UnicodeError
            findings.extend(scan_text(contents, location, denied))
        except (OSError, UnicodeError):
            findings.append(Finding(location, "unreadable-worktree"))

    for commit in git(root, "rev-list", "--all").decode().splitlines():
        counts["history_commits"] += 1
        contents = git(root, "cat-file", "commit", commit).decode("utf-8")
        findings.extend(scan_text(contents, f"commit:{commit}", denied))
        for entry in git(root, "ls-tree", "-r", "-z", "--full-tree", commit).split(b"\0"):
            if not entry:
                continue
            metadata, path = entry.split(b"\t", 1)
            mode, kind, oid = metadata.decode().split()
            location = f"history:{commit}:{oid}"
            findings.extend(scan_path(os.fsdecode(path), location, denied))
            if kind != "blob" or mode not in {"100644", "100755"}:
                findings.append(Finding(location, "unsupported-history-entry"))
            else:
                blob(oid, f"blob:{oid}")
    # Annotated tag messages are publication content too.
    for entry in git(root, "for-each-ref", "--format=%(objecttype) %(objectname) %(refname)").decode().splitlines():
        kind, oid, name = entry.split(" ", 2)
        findings.extend(scan_text(name, f"ref:{oid}", denied))
        if kind == "tag":
            findings.extend(scan_text(git(root, "cat-file", "tag", oid).decode(), f"tag:{oid}", denied))
    counts["unique_blobs"] = len(blobs)
    return sorted(set(findings), key=lambda item: (item.location, item.rule, item.line)), counts


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--deny-file", type=Path, help="private UTF-8 file of additional literal values, one per line")
    args = parser.parse_args(argv)
    try:
        denied = tuple(value.strip() for value in args.deny_file.read_text(encoding="utf-8").splitlines()
                       if value.strip()) if args.deny_file else ()
        if any(len(value) < 4 for value in denied):
            raise ValueError("Deny values must contain at least four characters")
        findings, counts = scan_repository(args.root, denied)
    except (OSError, ValueError, UnicodeError, subprocess.SubprocessError):
        print("FAIL: repository scan could not complete; full local Git history is required.", file=sys.stderr)
        return 2
    for finding in findings:
        print(f"FAIL {finding.location} line={finding.line} rule={finding.rule}")
    print(f"Scanned {counts['tracked_files']} tracked files, {counts['history_commits']} commits, "
          f"{counts['unique_blobs']} unique blobs: {len(findings)} findings.")
    return int(bool(findings))


if __name__ == "__main__":
    raise SystemExit(main())
