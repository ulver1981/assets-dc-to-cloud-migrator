"""Offline safety checks for a directory intended for public distribution."""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


PROHIBITED_COMPONENTS = {
    ".venv", "__pycache__", "exports", "logs", "mappings", ".git",
}
PRIVATE_HOST_MARKERS = ("tagetik", "internal", "corp", "intranet", "localhost")
GENERIC_HOSTS = {"example.com", "api.atlassian.com", "id.atlassian.com"}
SECRET_KEY_PATTERN = re.compile(
    r"(?im)^\s*[A-Z][A-Z0-9_]*(?:TOKEN|PASSWORD|SECRET|API_KEY)\s*=\s*(.+?)\s*$"
)
URL_PATTERN = re.compile(r"https?://[^\s'\"<>]+")


@dataclass(frozen=True)
class Finding:
    path: Path
    category: str


def inspect_release_tree(root: Path) -> list[Finding]:
    """Return public-release blockers without returning suspected secret values."""
    findings: list[Finding] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if _has_prohibited_component(relative):
            findings.append(Finding(relative, "prohibited path"))
            continue
        if path.is_file() and path.name != ".env.template":
            if path.name == ".env" or path.name.startswith(".env."):
                findings.append(Finding(relative, "environment file"))
                continue
        if not path.is_file() or _is_binary(path):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if _contains_likely_secret(text):
            findings.append(Finding(relative, "likely credential"))
        if _contains_non_generic_endpoint(text):
            findings.append(Finding(relative, "non-generic endpoint"))
    return findings


def _has_prohibited_component(path: Path) -> bool:
    if path.parts and path.parts[0] == "fixtures":
        return False
    return any(part in PROHIBITED_COMPONENTS for part in path.parts)


def _is_binary(path: Path) -> bool:
    return b"\0" in path.read_bytes()[:1024]


def _contains_likely_secret(text: str) -> bool:
    for match in SECRET_KEY_PATTERN.finditer(text):
        value = match.group(1).strip().strip("'\"")
        if value and not _is_placeholder(value):
            return True
    return bool(re.search(r"(?i)\b(?:bearer|basic)\s+[a-z0-9._~+/=-]{12,}", text))


def _is_placeholder(value: str) -> bool:
    normalized = value.lower()
    return any(marker in normalized for marker in (
        "your_", "your-", "your ", "example", "placeholder", "<", "xxxx",
    ))


def _contains_non_generic_endpoint(text: str) -> bool:
    for candidate in URL_PATTERN.findall(text):
        host = (urlparse(candidate).hostname or "").lower()
        if any(marker in host for marker in PRIVATE_HOST_MARKERS):
            return True
        if (host and host not in GENERIC_HOSTS and not host.endswith(".example.com")
                and not host.endswith(".test")):
            return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check a prospective public release directory offline."
    )
    parser.add_argument("root", type=Path, nargs="?", default=Path("."))
    args = parser.parse_args()
    findings = inspect_release_tree(args.root.resolve())
    if not findings:
        print("Public-release readiness check passed.")
        return 0
    print("Public-release readiness check failed:")
    for finding in findings:
        print(f" - [{finding.category}] {finding.path.as_posix()}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
