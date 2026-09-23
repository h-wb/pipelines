"""GitHub account-export source (one-shot import).

Reads an extracted "Export account data" archive: the JSON record files
(pull_requests_*.json, issues_*.json, ...) and the bare git repos under
repositories/<owner>/<name>.git. The export covers everything inside repos
you own (any author, bots included) plus full git history; the live `github`
source covers your own activity anywhere, so the two complement each other.

Records keep the export's URL identifiers; join them to the API tables on
`url` = html_url.
"""

import glob
import json
import os
import subprocess
from typing import Any, Dict, Iterator, List, Optional, Set
from urllib.parse import unquote

import dlt
from dlt.extract import DltResource

FIELD_SEP, RECORD_SEP = "\x1f", "\x1e"


def _load(export_dir: str, kind: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for path in sorted(glob.glob(os.path.join(export_dir, f"{kind}_*.json"))):
        with open(path) as f:
            rows += json.load(f)
    return rows


def _login(user_url: Optional[str]) -> Optional[str]:
    return user_url.rstrip("/").rsplit("/", 1)[-1] if user_url else None


def _repo(url: str) -> str:
    return "/".join(url.split("/")[3:5])


def _git(git_dir: str, *args: str) -> str:
    return subprocess.run(
        ["git", f"--git-dir={git_dir}", *args],
        capture_output=True, text=True, check=True, errors="replace",
    ).stdout


@dlt.source(name="github_export")
def github_export_source(
    export_dir: str = dlt.config.value,
    author_emails: List[str] = dlt.secrets.value,
    author_login: str = "h-wb",
    fork_repos: Optional[List[str]] = None,
) -> Iterator[DltResource]:
    """Args:
        export_dir: path of the extracted export archive
        author_emails: commit author emails that count as "mine"
        author_login: GitHub login (also matched for commits with an empty email)
        fork_repos: "owner/name" repos that are forks; only my commits are kept there
    """
    mine_emails: Set[str] = {e.lower() for e in author_emails}
    forks = set(fork_repos or [])
    bots = {_login(b["url"]) for b in _load(export_dir, "bots")}

    def is_bot(login: Optional[str]) -> bool:
        return bool(login) and (login in bots or login.endswith("[bot]"))

    def actor(record: Dict[str, Any], key: str = "user") -> Dict[str, Any]:
        login = _login(record.get(key))
        return {"author": login, "is_bot": is_bot(login)}

    def labels(record: Dict[str, Any]) -> List[str]:
        return [unquote(u.rsplit("/labels/", 1)[-1]) for u in record.get("labels") or []]

    @dlt.resource(write_disposition="replace", primary_key=("repository", "sha"))
    def commits() -> Iterator[List[Dict[str, Any]]]:
        fmt = FIELD_SEP.join(["%H", "%an", "%ae", "%aI", "%cn", "%ce", "%cI", "%P", "%B"]) + RECORD_SEP
        for git_dir in sorted(glob.glob(os.path.join(export_dir, "repositories", "*", "*.git"))):
            owner = os.path.basename(os.path.dirname(git_dir))
            repo = f"{owner}/{os.path.basename(git_dir)[:-4]}"

            stats: Dict[str, Dict[str, int]] = {}
            sha = None
            for line in _git(git_dir, "log", "--all", "--numstat", "--format=%x00%H").splitlines():
                if line.startswith("\x00"):
                    sha = line[1:]
                    stats[sha] = {"additions": 0, "deletions": 0, "files_changed": 0}
                elif line.strip() and sha:
                    added, deleted, _ = line.split("\t", 2)
                    s = stats[sha]
                    s["files_changed"] += 1
                    # binary files show "-" for both counts
                    s["additions"] += int(added) if added.isdigit() else 0
                    s["deletions"] += int(deleted) if deleted.isdigit() else 0

            rows = []
            for record in _git(git_dir, "log", "--all", f"--format={fmt}").split(RECORD_SEP):
                fields = record.strip("\n").split(FIELD_SEP)
                if len(fields) != 9:
                    continue
                sha, an, ae, at, cn, ce, ct, parents, message = fields
                mine = ae.lower() in mine_emails or (not ae and an == author_login)
                if repo in forks and not mine:
                    continue  # forked upstream history: only keep my own commits
                rows.append({
                    "repository": repo,
                    "sha": sha,
                    "author_name": an,
                    "author_email": ae,
                    "authored_at": at,
                    "committer_name": cn,
                    "committer_email": ce,
                    "committed_at": ct,
                    "parent_count": len(parents.split()),
                    "message": message.strip(),
                    "is_mine": mine,
                    "is_bot": "[bot]" in ae or an.endswith("[bot]"),
                    "is_fork_repo": repo in forks,
                    **stats.get(sha, {}),
                })
            yield rows

    @dlt.resource(write_disposition="replace", primary_key="url")
    def pull_requests() -> Iterator[Dict[str, Any]]:
        for p in _load(export_dir, "pull_requests"):
            yield {
                "url": p["url"],
                "repository": _repo(p["url"]),
                "number": int(p["url"].rsplit("/", 1)[-1]),
                **actor(p),
                "title": p["title"],
                "body": p["body"],
                "base_ref": (p.get("base") or {}).get("ref"),
                "head_ref": (p.get("head") or {}).get("ref"),
                "labels": labels(p),
                "draft": p.get("work_in_progress"),
                "created_at": p["created_at"],
                "merged_at": p.get("merged_at"),
                "closed_at": p.get("closed_at"),
                "merge_commit_sha": p.get("merge_commit_sha"),
            }

    @dlt.resource(write_disposition="replace", primary_key="url")
    def issues() -> Iterator[Dict[str, Any]]:
        for i in _load(export_dir, "issues"):
            yield {
                "url": i["url"],
                "repository": _repo(i["url"]),
                "number": int(i["url"].rsplit("/", 1)[-1]),
                **actor(i),
                "title": i["title"],
                "body": i["body"],
                "labels": labels(i),
                "created_at": i["created_at"],
                "updated_at": i.get("updated_at"),
                "closed_at": i.get("closed_at"),
            }

    @dlt.resource(write_disposition="replace", primary_key="url")
    def issue_comments() -> Iterator[Dict[str, Any]]:
        for c in _load(export_dir, "issue_comments"):
            parent = c.get("pull_request") or c.get("issue")
            yield {
                "url": c["url"],
                "repository": _repo(c["url"]),
                "parent_url": parent,
                "parent_type": "pull_request" if c.get("pull_request") else "issue",
                **actor(c),
                "body": c["body"],
                "created_at": c["created_at"],
            }

    @dlt.resource(write_disposition="replace", primary_key="url")
    def pull_request_reviews() -> Iterator[Dict[str, Any]]:
        for r in _load(export_dir, "pull_request_reviews"):
            yield {
                "url": r["url"],
                "repository": _repo(r["url"]),
                "pull_request_url": r["pull_request"],
                **actor(r),
                # raw export code; the archive doesn't document its mapping
                "state_code": r.get("state"),
                "body": r["body"],
                "created_at": r["created_at"],
                "submitted_at": r.get("submitted_at"),
            }

    @dlt.resource(write_disposition="replace", primary_key="url")
    def issue_events() -> Iterator[Dict[str, Any]]:
        for e in _load(export_dir, "issue_events"):
            parent = e.get("pull_request") or e.get("issue")
            yield {
                "url": e["url"],
                "repository": _repo(e["url"]),
                "parent_url": parent,
                "parent_type": "pull_request" if e.get("pull_request") else "issue",
                **actor(e, "actor"),
                "event": e["event"],
                "label_name": e.get("label_name"),
                "commit_id": e.get("commit_id"),
                "title_was": e.get("title_was"),
                "title_is": e.get("title_is"),
                "created_at": e["created_at"],
            }

    @dlt.resource(write_disposition="replace", primary_key="url")
    def releases() -> Iterator[Dict[str, Any]]:
        for r in _load(export_dir, "releases"):
            yield {
                "url": r["url"],
                "repository": _repo(r["url"]),
                **actor(r),
                "name": r.get("name"),
                "tag_name": r.get("tag_name"),
                "body": r.get("body"),
                "prerelease": r.get("prerelease"),
                "created_at": r["created_at"],
                "published_at": r.get("published_at"),
            }

    return (
        commits,
        pull_requests,
        issues,
        issue_comments,
        pull_request_reviews,
        issue_events,
        releases,
    )
