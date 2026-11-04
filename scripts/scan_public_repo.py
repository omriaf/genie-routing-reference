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
import shlex
import subprocess
import sys


_SECRET_KEYS = (
    r"(?:DATABRICKS_|GITHUB_|ARM_|AZURE_)?(?:TOKEN|ACCESS_TOKEN|REFRESH_TOKEN|CLIENT_SECRET|"
    r"PASSWORD|API_KEY|SECRET_VALUE)|GITHUB_DEFINITIONS_TOKEN"
)
_PROFILE_KEYS = r"profile|DATABRICKS_CONFIG_PROFILE"


def _quoted_assignments(keys: str) -> str:
    # Preserve source-literal detection, independently of configuration grammar.
    key = (r"(?:\"(?:" + keys + r")\"|'(?:" + keys + r")'|(?:" + keys
           + r"))[ \t]*[:=][ \t]*")
    return r"(?<![\w.])" + key + r"[\"']([^\"'\r\n]+)[\"']"


_ASSIGNMENT_KEYS = {
    "secret-literal": re.compile(r"(?:" + _SECRET_KEYS + r")\Z", re.IGNORECASE),
    "profile-literal": re.compile(r"(?:" + _PROFILE_KEYS + r")\Z", re.IGNORECASE),
}
_KEY_HINT = re.compile(r"\b(?:" + _SECRET_KEYS + "|" + _PROFILE_KEYS + r")[\"']?[ \t]*[:=]", re.IGNORECASE)
_QUOTED_WORD = r'"(?:\\.|[^"\\])*"' + "|" + r"'[^']*'"
_CONFIG_TOKENS = re.compile(_QUOTED_WORD + r"|[{}\[\],:=()]|[^\s{}\[\],:=()]+")
_BARE_SCALAR = re.compile(r"[^\s\"'`,;{}()[\]&|]+")
# Shell words retain quoting/escaping until shlex decodes one word. Operators
# are separate tokens even without spaces. Nothing is expanded or executed.
_SHELL_TOKENS = re.compile(
    r'''&&|\|\||[;&|()]|(?:[^\s;&|()'"\\]+|\\(?:\r?\n|[^\r\n])|"(?:\\[\s\S]|[^"\\])*"|'[^']*')+'''
)
_SHELL_PREFIXES = {"env", "export", "readonly", "declare", "typeset", "local",
                   "exec", "command", "builtin", "sudo", "nohup", "time"}
_DECLARATIONS = {"export", "readonly", "declare", "typeset", "local"}
_CONTROL_PREFIXES = {"if", "then", "elif", "else", "while", "until", "do", "!", "{"}
_OPTION_ARGUMENTS = {
    "env": {"-u", "--unset", "-C", "--chdir"},
    "sudo": {"-u", "--user", "-g", "--group", "-h", "--host", "-p", "--prompt"},
    "time": {"-f", "--format", "-o", "--output"},
    "exec": {"-a"},
}


def _assignment_rule(key: str) -> str | None:
    for rule, pattern in _ASSIGNMENT_KEYS.items():
        if pattern.fullmatch(key):
            return rule
    return None


def _logical_lines(contents: str):
    start, pending = 0, ""
    for line in contents.splitlines(keepends=True):
        pending += line
        if line.endswith(("\\\n", "\\\r\n")):
            continue
        yield start, pending
        start += len(pending)
        pending = ""
    if pending:
        yield start, pending


def _mapping_assignments(line: str):
    tokens = list(_CONFIG_TOKENS.finditer(line))
    containers = []
    for index, token in enumerate(tokens):
        word = token[0]
        if word.startswith("#"):
            break
        if word in {"{", "["}:
            containers.append(word)
        elif word in {"}", "]"} and containers:
            containers.pop()
        key = word[1:-1] if word.startswith(('"', "'")) else word
        rule = _assignment_rule(key)
        if rule is None or index + 1 == len(tokens) or tokens[index + 1][0] not in {":", "="}:
            continue
        # A block/list mapping key, or a key within a flow collection. A comma
        # in ordinary prose or a Python call is not a mapping boundary.
        block_key = all(previous[0] == "-" for previous in tokens[:index])
        flow_key = bool(containers) and index > 0 and tokens[index - 1][0] in {"{", "[", ","}
        if not (block_key or flow_key):
            continue
        value_start = tokens[index + 1].end()
        while value_start < len(line) and line[value_start] in " \t":
            value_start += 1
        value = _BARE_SCALAR.match(line, value_start)
        if value is None or (value.end() < len(line) and line[value.end()] == "("):
            continue  # Quoted literals are checked separately; calls are not scalars.
        scalar = value[0]
        if tokens[index + 1][0] == ":":
            # YAML plain scalars can contain spaces. Only the whole value may
            # equal a placeholder; a safe first word cannot hide private text.
            scalar = line[value_start:].splitlines()[0]
            if flow_key:
                scalar = re.split(r"[,}\]]", scalar, maxsplit=1)[0]
            scalar = re.split(r"[ \t]+#", scalar, maxsplit=1)[0].strip()
        yield rule, scalar, True, token.start()


def _shell_assignments(line: str):
    tokens = list(_SHELL_TOKENS.finditer(line))
    assignment_position, prefix, option_argument = True, None, False
    for index, token in enumerate(tokens):
        raw = token[0]
        if raw.startswith("#"):
            break
        if raw in {"\\\n", "\\\r\n"}:
            continue
        if raw in {";", "&&", "||", "|", "&"}:
            assignment_position, prefix, option_argument = True, None, False
            continue
        if raw == "(":
            continue  # Grouping preserves command position; function calls do not reset it.
        if raw == ")":
            assignment_position = False
        if not assignment_position:
            continue
        if raw in _CONTROL_PREFIXES:
            prefix, option_argument = None, False
            continue
        try:
            words = shlex.split(raw, comments=False, posix=True)
        except ValueError:
            continue
        if len(words) != 1:
            continue
        word = words[0]
        if option_argument:
            option_argument = False
            continue
        if prefix and word.startswith("-"):
            option_argument = word in _OPTION_ARGUMENTS.get(prefix, set())
            continue
        key, separator, value = word.partition("=")
        if separator and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) and (
            prefix == "env" or not raw.startswith(('"', "'"))
        ):
            # Parentheses adjacent to an unquoted value indicate a source call,
            # not a syntactically valid shell assignment.
            if index + 1 < len(tokens) and tokens[index + 1][0] == "(" and token.end() == tokens[index + 1].start():
                assignment_position = False
                continue
            rule = _assignment_rule(key)
            if rule is not None and value:
                raw_value = raw.partition("=")[2]
                yield rule, value, not raw_value.startswith(('"', "'")), token.start()
            continue  # Subsequent assignments remain in command position.
        command = word.rsplit("/", 1)[-1]
        if command in _SHELL_PREFIXES:
            prefix = command
        elif prefix not in _DECLARATIONS or not word.isidentifier():
            assignment_position = False


def _structured_assignment_findings(contents: str, location: str):
    for start, line in _logical_lines(contents):
        if not _KEY_HINT.search(line):
            continue
        for parse in (_mapping_assignments, _shell_assignments):
            for rule, value, bare, offset in parse(line):
                if (bare and value in {"null", "None", "~"}) or value in SYNTHETIC[rule]:
                    continue
                yield Finding(location, rule, contents.count("\n", 0, start + offset) + 1)


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
    "secret-literal": re.compile(_quoted_assignments(_SECRET_KEYS), re.IGNORECASE),
    "profile-literal": re.compile(
        r"(?m:^(?:\s*|Examples: )(?:python|databricks)[^\n]*?--profile[ =]+[\"']?([A-Za-z0-9_.-]+))|"
        r"--profile[\"']\s*,\s*[\"']([A-Za-z0-9_<][^\"'\n]*)[\"']|"
        + _quoted_assignments(_PROFILE_KEYS), re.IGNORECASE,
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
                    match.end() == len(contents) or contents[match.end()] in " \t\r\n,]}):;#&|"
                ):
                    continue
            findings.append(Finding(location, rule, contents.count("\n", 0, match.start()) + 1))
    findings.extend(_structured_assignment_findings(contents, location))
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
