#!/usr/bin/env python3
"""
push_update.py — Push only changed P2P skill files to GitHub
=============================================================
Compares each local file against its GitHub version using SHA hash.
Only pushes files that have actually changed — never overwrites unchanged files.

Usage (from inside Claude Code):
  python push_update.py                        # push all changed files
  python push_update.py --dry-run              # show what would change, don't push
  python push_update.py --files src/oracle_retry.py RETRY_GUIDE.md   # push specific files
  python push_update.py --branch feature/retry-module                  # push to a branch

Requires:
  pip install PyGithub python-dotenv
  GITHUB_PAT env var set (in .env or shell)
  GITHUB_REPO env var set (e.g. "your-org/p2p-skill-package")
"""

import argparse
import base64
import hashlib
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

try:
    from github import Github, GithubException
except ImportError:
    print("ERROR: Run:  pip install PyGithub python-dotenv")
    sys.exit(1)


# ── Config ────────────────────────────────────────────────────────────────────

GITHUB_PAT  = os.getenv("GITHUB_PAT")
GITHUB_REPO = os.getenv("GITHUB_REPO")   # e.g. "your-org/p2p-skill-package"
BRANCH      = os.getenv("GITHUB_BRANCH", "main")

# All files this project manages — relative to this script's directory
MANAGED_FILES = [
    "CLAUDE.md",
    "RETRY_GUIDE.md",
    "skills/PR1_SUPPLIER_REGISTRATION.md",
    "skills/PR2_REQUISITION.md",
    "skills/PR3_SOURCING_NEGOTIATION.md",
    "skills/PR4_AGREEMENT.md",
    "skills/PR5_PURCHASE_ORDER.md",
    "skills/PR6_RECEIVING.md",
    "skills/PR7_LIFECYCLE_MONITOR.md",
    "config/config.yaml",
    "config/.env.example",
    "samples/all_agents_sample_requests.json",
    "src/oracle_retry.py",
    "src/oracle_retry_usage.py",
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def sha_of_local(path: Path) -> str:
    """Compute the same SHA GitHub uses internally for blob comparison."""
    content = path.read_bytes()
    header  = f"blob {len(content)}\0".encode()
    return hashlib.sha1(header + content).hexdigest()


def get_remote_file(repo, path: str, branch: str):
    """Return GitHub ContentFile or None if file doesn't exist yet."""
    try:
        return repo.get_contents(path, ref=branch)
    except GithubException as e:
        if e.status == 404:
            return None
        raise


def push_file(repo, local_path: Path, remote_path: str,
              branch: str, dry_run: bool) -> str:
    """
    Push one file. Returns status string:
      CREATED   — new file, didn't exist on GitHub
      UPDATED   — file existed and was different
      UNCHANGED — file identical, skipped
    """
    local_sha  = sha_of_local(local_path)
    remote     = get_remote_file(repo, remote_path, branch)

    if remote and remote.sha == local_sha:
        return "UNCHANGED"

    content = local_path.read_bytes()
    message = (
        f"feat: add {local_path.name}"
        if remote is None
        else f"fix: update {local_path.name}"
    )

    if dry_run:
        return "CREATED (dry-run)" if remote is None else "UPDATED (dry-run)"

    if remote is None:
        repo.create_file(
            path    = remote_path,
            message = message,
            content = content,
            branch  = branch,
        )
        return "CREATED"
    else:
        repo.update_file(
            path    = remote_path,
            message = message,
            content = content,
            sha     = remote.sha,
            branch  = branch,
        )
        return "UPDATED"


def ensure_branch(repo, branch: str) -> None:
    """Create branch from main if it doesn't exist."""
    if branch == "main":
        return
    try:
        repo.get_branch(branch)
    except GithubException as e:
        if e.status == 404:
            main_sha = repo.get_branch("main").commit.sha
            repo.create_git_ref(f"refs/heads/{branch}", main_sha)
            print(f"  Created branch: {branch}")
        else:
            raise


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Push changed P2P skill files to GitHub")
    parser.add_argument("--dry-run",  action="store_true", help="Show changes without pushing")
    parser.add_argument("--branch",   default=BRANCH,      help="Target branch (default: main)")
    parser.add_argument("--files",    nargs="+",           help="Push specific files only")
    args = parser.parse_args()

    # Validate config
    if not GITHUB_PAT:
        print("ERROR: GITHUB_PAT not set. Add it to .env or export it in your shell.")
        sys.exit(1)
    if not GITHUB_REPO:
        print("ERROR: GITHUB_REPO not set. Example: your-org/p2p-skill-package")
        sys.exit(1)

    # Connect
    g    = Github(GITHUB_PAT)
    repo = g.get_repo(GITHUB_REPO)
    ensure_branch(repo, args.branch)

    # Resolve which files to process
    root       = Path(__file__).parent
    file_list  = args.files if args.files else MANAGED_FILES

    print(f"\n{'DRY RUN — ' if args.dry_run else ''}Pushing to {GITHUB_REPO} @ {args.branch}\n")
    print(f"  {'File':<52} {'Status'}")
    print(f"  {'-'*52} {'-'*12}")

    counts = {"CREATED": 0, "UPDATED": 0, "UNCHANGED": 0, "MISSING": 0, "ERROR": 0}

    for rel_path in file_list:
        local = root / rel_path
        if not local.exists():
            print(f"  {rel_path:<52} MISSING (local file not found)")
            counts["MISSING"] += 1
            continue
        try:
            status = push_file(
                repo        = repo,
                local_path  = local,
                remote_path = rel_path,
                branch      = args.branch,
                dry_run     = args.dry_run,
            )
            print(f"  {rel_path:<52} {status}")
            key = status.split(" ")[0]          # strip "(dry-run)" suffix
            counts[key] = counts.get(key, 0) + 1
        except Exception as e:
            print(f"  {rel_path:<52} ERROR — {e}")
            counts["ERROR"] += 1

    # Summary
    print(f"\n  Created: {counts['CREATED']}  "
          f"Updated: {counts['UPDATED']}  "
          f"Unchanged: {counts['UNCHANGED']}  "
          f"Missing: {counts['MISSING']}  "
          f"Errors: {counts['ERROR']}")

    if counts["CREATED"] + counts["UPDATED"] > 0 and not args.dry_run:
        print(f"\n  View on GitHub: https://github.com/{GITHUB_REPO}/tree/{args.branch}")

    if counts["ERROR"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
