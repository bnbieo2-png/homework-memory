#!/usr/bin/env python3
"""Fail closed on publishable private data; never print matched secret values.

Optional HOMEWORK_PRIVACY_MARKERS_FILE points to an external UTF-8 file with one
private marker per line. Keep that file outside the repository. --history also
checks every reachable commit, including deleted files and author/committer mail.
"""
from __future__ import annotations

import argparse
import ast
import ipaddress
import hashlib
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BINARY_SUFFIXES = {'.db', '.eml', '.heic', '.jpeg', '.jpg', '.log', '.pdf', '.png', '.sqlite', '.sqlite3', '.webp'}
PRIVATE_PARTS = {'data', 'email_exports', 'logs', 'source-images', 'question-images'}
IGNORED_TOOL_DIRS = {'.git', '.gstack', '.pytest_cache', '.venv', '__pycache__'}
EMAIL_PATTERN = re.compile(r'(?<![\w.+-])[\w.+-]+@(?:[\w-]+\.)+[A-Za-z]{2,}')
HOME_PATTERN = re.compile(r'(?:/(?:Users|home)/[^/\s\"\']+|[A-Za-z]:\\Users\\[^\\\s\"\']+)', re.I)
IP_PATTERN = re.compile(r'(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])')
DOCUMENTATION_NETS = tuple(ipaddress.ip_network(net) for net in ('192.0.2.0/24', '198.51.100.0/24', '203.0.113.0/24'))
CREDENTIAL_PATTERNS = (
    re.compile(r'\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16})\b'),
    re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    re.compile(r'''(?i)\b(?:api[_-]?key|api[_-]?secret|password|access[_-]?token|auth[_-]?token)\b[\"']?\s*[:=]\s*[\"']([^\"'\n]{8,})[\"']'''),
)
# Reviewed synthetic credentials in the existing HTTP authentication tests only.
TEST_CREDENTIAL_HASHES = {'72a907006208f08876a3508d0677a37844ec35b7f1877120692af2b45dddb30c', '74841537f14bd5ecb95d96f700df0b86d93e32e42d5483b27d1690e3f2c1bcf0'}
PLACEHOLDERS = {'changeme', 'your-password', 'your-api-key', 'your_api_key', 'your_password', 'example', 'placeholder'}


def git(root: Path, *args: str) -> bytes:
    return subprocess.run(['git', *args], cwd=root, check=True, capture_output=True).stdout


def is_repository(root: Path) -> bool:
    return (root / '.git').exists()


def candidate_files(root: Path = ROOT) -> list[Path]:
    if is_repository(root):
        names = git(root, 'ls-files', '-z', '--cached', '--others', '--exclude-standard').split(b'\0')
        return sorted({root / os.fsdecode(name) for name in names if name})
    return sorted(path for path in root.rglob('*') if path.is_file() and not any(part in IGNORED_TOOL_DIRS for part in path.relative_to(root).parts))


def allowed_email(address: str) -> bool:
    domain = address.rsplit('@', 1)[-1].lower()
    return domain in {'example.com', 'example.org', 'example.net', 'users.noreply.github.com', 'noreply.github.com'} or domain.endswith('.test') or domain.endswith('.invalid')


def string_value(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = string_value(node.left), string_value(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def scan_text(text: str, label: str, markers: tuple[str, ...] = (), python: bool = False) -> list[str]:
    lines = list(enumerate(text.splitlines(), 1))
    if python:
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError, RecursionError):
            return [f'cannot parse Python: {label}']
        for node in ast.walk(tree):
            value = string_value(node)
            if value is not None:
                lines.append((getattr(node, 'lineno', 1), value))
    findings = []
    for number, line in lines:
        kinds = set()
        if any(marker.casefold() in line.casefold() for marker in markers):
            kinds.add('private marker')
        if HOME_PATTERN.search(line):
            kinds.add('absolute home path')
        if any(not allowed_email(match.group()) for match in EMAIL_PATTERN.finditer(line)):
            kinds.add('email address')
        for value in IP_PATTERN.findall(line):
            try:
                address = ipaddress.ip_address(value)
            except ValueError:
                continue
            if not (address.is_loopback or address.is_unspecified or any(address in net for net in DOCUMENTATION_NETS)):
                kinds.add('non-local IP address')
        for pattern in CREDENTIAL_PATTERNS:
            for match in pattern.finditer(line):
                value = match.group(1) if match.lastindex else match.group()
                fixture = label.split(':', 1)[-1].endswith('test_homework_system.py') and hashlib.sha256(value.encode()).hexdigest() in TEST_CREDENTIAL_HASHES
                if value.lower() not in PLACEHOLDERS and not value.startswith('${') and not fixture:
                    kinds.add('possible credential')
        findings.extend(f'{kind}: {label}:{number}' for kind in sorted(kinds))
    return findings


def scan_file(name: str, data: bytes, label: str, markers: tuple[str, ...]) -> list[str]:
    path = Path(name)
    if (path.name == '.env' or path.name.startswith('.env.') and path.name != '.env.example'
            or any(part in PRIVATE_PARTS for part in path.parts)):
        return [f'private path: {label}']
    if path.suffix.lower() in BINARY_SUFFIXES:
        return [f'private file type: {label}']
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        return [f'unreviewed binary file: {label}']
    return scan_text(text, label, markers, python=path.suffix == '.py')


def audit(root: Path, history: bool = False, markers: tuple[str, ...] = ()) -> tuple[list[str], int]:
    findings = []
    files = candidate_files(root)
    for path in files:
        name = str(path.relative_to(root))
        if path.is_symlink():
            findings.append(f'unreviewed symbolic link: {name}')
            continue
        try:
            data = path.read_bytes()
        except OSError:
            findings.append(f'unreadable file: {name}')
            continue
        findings.extend(scan_file(name, data, name, markers))
    if history:
        if not is_repository(root):
            findings.append('history requested but no repository found')
        else:
            seen = set()
            commits = git(root, 'rev-list', '--all').decode().splitlines()
            if not commits:
                findings.append('history requested but no commits found')
            for commit in commits:
                label = f'commit {commit[:12]}'
                emails = git(root, 'show', '-s', '--format=%ae%n%ce', commit).decode().splitlines()
                for role, email in zip(('author', 'committer'), emails):
                    if not allowed_email(email):
                        findings.append(f'private {role} email: {label}')
                metadata = git(root, 'show', '-s', '--format=%an%n%cn%n%B', commit).decode('utf-8', errors='replace')
                findings.extend(scan_text(metadata, label, markers))
                for entry in git(root, 'ls-tree', '-rz', commit).split(b'\0'):
                    if not entry:
                        continue
                    descriptor, name_bytes = entry.split(b'\t', 1)
                    mode, kind, oid = descriptor.decode().split()
                    name = os.fsdecode(name_bytes)
                    if (name, oid) in seen:
                        continue
                    seen.add((name, oid))
                    location = f'{label}/{name}'
                    if kind != 'blob' or mode == '120000':
                        findings.append(f'unreviewed linked content: {location}')
                        continue
                    findings.extend(scan_file(name, git(root, 'cat-file', 'blob', oid), location, markers))
    return sorted(set(findings)), len(files)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--history', action='store_true', help='Check all reachable Git history, including commit identities.')
    parser.add_argument('--root', type=Path, default=ROOT)
    args = parser.parse_args()
    try:
        marker_path = os.environ.get('HOMEWORK_PRIVACY_MARKERS_FILE')
        markers = ()
        if marker_path:
            path = Path(marker_path).expanduser().resolve()
            if path.is_relative_to(args.root.resolve()):
                raise ValueError('private marker file must be outside repository')
            markers = tuple(line.strip() for line in path.read_text(encoding='utf-8').splitlines() if line.strip() and not line.startswith('#'))
        findings, count = audit(args.root.resolve(), args.history, markers)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f'Privacy audit could not finish ({type(error).__name__}); do not publish.')
        return 2
    if findings:
        print('Privacy audit failed:')
        for finding in findings:
            print(f'- {finding}')
        return 1
    print(f'Privacy audit passed: checked {count} current files' + (' and all reachable Git history.' if args.history else '.'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
