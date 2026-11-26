#!/usr/bin/env python3
"""Pass engineering-authored PRs; otherwise require two current-head
engineering approvals. Bot-authored PRs (user type Bot) are skipped.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
API_VERSION = "2022-11-28"

ORG = os.environ["ORG"]
ENGINEERING_TEAM = os.environ["ENGINEERING_TEAM"]
REQUIRED = int(os.environ["REQUIRED_ENG_APPROVALS"])
TEAM_TOKEN = os.environ.get("TEAM_TOKEN", "")
GITHUB_API_TOKEN = os.environ["GITHUB_API_TOKEN"]
AUTHOR = os.environ.get("AUTHOR", "")
AUTHOR_TYPE = os.environ.get("AUTHOR_TYPE", "")
PR_NUMBER = os.environ.get("PR_NUMBER", "")
HEAD_SHA = os.environ.get("HEAD_SHA", "")
REPO = os.environ["REPO"]


class HttpError(Exception):
    def __init__(self, status, body, url):
        self.status = status
        self.body = body
        self.url = url
        super().__init__(f"HTTP {status} for {url}")


def fail(message: str) -> None:
    print(f"::error::{message}")
    raise SystemExit(1)


def request(url: str, token: str):
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", API_VERSION)
    req.add_header("User-Agent", "plurel-non-eng-engineering-approvals")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            data = json.loads(raw) if raw else None
            return data, resp.headers.get("Link", ""), resp.status
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise HttpError(exc.code, body, url) from exc


def next_link(link_header: str):
    if not link_header:
        return None
    for part in link_header.split(","):
        if 'rel="next"' in part:
            start = part.find("<") + 1
            end = part.find(">", start)
            return part[start:end]
    return None


def paginate(url: str, token: str):
    items = []
    while url:
        data, link, _ = request(url, token)
        if not isinstance(data, list):
            fail(f"Expected a list from {url}")
        items.extend(data)
        url = next_link(link)
    return items


def require_team(slug: str) -> None:
    url = f"{API}/orgs/{ORG}/teams/{slug}"
    try:
        request(url, TEAM_TOKEN)
    except HttpError as exc:
        if exc.status == 404:
            fail(
                f"Organization team '{slug}' was not found in {ORG}. "
                "Create it (closed/visible, not secret) and add its members "
                "before this check can run."
            )
        if exc.status in (401, 403):
            fail(
                "ORG_TEAM_READ_TOKEN cannot read organization teams. "
                "Grant organization Members: Read (classic: read:org) and "
                "confirm the token is an org or repo Actions secret named "
                "ORG_TEAM_READ_TOKEN."
            )
        fail(f"Failed to load team '{slug}': HTTP {exc.status}")


def is_active_member(slug: str, login: str) -> bool:
    url = f"{API}/orgs/{ORG}/teams/{slug}/memberships/{login}"
    try:
        data, _, _ = request(url, TEAM_TOKEN)
    except HttpError as exc:
        if exc.status == 404:
            return False
        if exc.status in (401, 403):
            fail(
                "ORG_TEAM_READ_TOKEN cannot read team membership. "
                "Grant organization Members: Read (classic: read:org)."
            )
        fail(f"Failed to read {slug} membership for {login}: HTTP {exc.status}")
    return bool(data) and data.get("state") == "active"


def team_logins(slug: str) -> set:
    url = f"{API}/orgs/{ORG}/teams/{slug}/members?per_page=100"
    try:
        members = paginate(url, TEAM_TOKEN)
    except HttpError as exc:
        if exc.status in (401, 403):
            fail(
                "ORG_TEAM_READ_TOKEN cannot list team members. "
                "Grant organization Members: Read (classic: read:org)."
            )
        fail(f"Failed to list members of '{slug}': HTTP {exc.status}")
    return {m["login"] for m in members if m.get("login")}


def resolve_pull_request():
    """Return (number, author, author_type, head_sha).

    pull_request events provide these in the environment. merge_group
    does not include a pull_request object, so look the PR up from the
    merge-group head SHA and use that PR's head for approval matching.
    """
    if PR_NUMBER and AUTHOR and HEAD_SHA:
        return int(PR_NUMBER), AUTHOR, AUTHOR_TYPE, HEAD_SHA
    if not HEAD_SHA:
        fail("Missing pull request context and HEAD_SHA; cannot evaluate reviews.")
    url = f"{API}/repos/{REPO}/commits/{urllib.parse.quote(HEAD_SHA, safe='')}/pulls"
    try:
        pulls = paginate(url, GITHUB_API_TOKEN)
    except HttpError as exc:
        fail(f"Failed to resolve a pull request for {HEAD_SHA}: HTTP {exc.status}")
    if not pulls:
        fail(f"No pull request found for commit {HEAD_SHA}.")
    open_pulls = [pr for pr in pulls if pr.get("state") == "open"] or pulls
    pr = open_pulls[0]
    user = pr.get("user") or {}
    head_sha = ((pr.get("head") or {}).get("sha")) or HEAD_SHA
    number = pr.get("number")
    login = user.get("login")
    if not number or not login:
        fail(f"Resolved pull request for {HEAD_SHA} is missing number or author.")
    return int(number), login, user.get("type") or "", head_sha


def latest_substantive_reviews(pr_number: int):
    url = f"{API}/repos/{REPO}/pulls/{pr_number}/reviews?per_page=100"
    try:
        reviews = paginate(url, GITHUB_API_TOKEN)
    except HttpError as exc:
        fail(f"Failed to list pull request reviews: HTTP {exc.status}")

    def sort_key(review):
        return (review.get("submitted_at") or "", review.get("id") or 0)

    latest = {}
    for review in sorted(reviews, key=sort_key):
        user = (review.get("user") or {}).get("login")
        state = review.get("state")
        if not user or state in ("COMMENTED", "PENDING") or not state:
            continue
        latest[user] = review
    return latest


def main() -> None:
    if not TEAM_TOKEN:
        fail(
            "Missing secret ORG_TEAM_READ_TOKEN. Add a GitHub App token or "
            "fine-grained PAT with organization Members: Read (classic: read:org) "
            "as an Actions secret of that name. The default GITHUB_TOKEN cannot "
            "read organization team membership."
        )

    if REQUIRED < 1:
        fail("REQUIRED_ENG_APPROVALS must be at least 1")

    require_team(ENGINEERING_TEAM)

    pr_number, author, author_type, head_sha = resolve_pull_request()

    if author_type == "Bot":
        print(
            f"Author {author} has type Bot; no engineering approvals required."
        )
        return

    if is_active_member(ENGINEERING_TEAM, author):
        print(
            f"Author {author} is an active member of {ORG}/{ENGINEERING_TEAM}; "
            "no extra engineering approvals required."
        )
        return

    engineers = team_logins(ENGINEERING_TEAM)
    engineers_lower = {login.lower(): login for login in engineers}
    latest = latest_substantive_reviews(pr_number)
    approved = []
    for login, review in latest.items():
        if login.lower() == author.lower():
            continue
        canonical = engineers_lower.get(login.lower())
        if not canonical:
            continue
        if review.get("state") != "APPROVED":
            continue
        if review.get("commit_id") != head_sha:
            print(
                f"Ignoring stale approval from {login} on "
                f"{review.get('commit_id')}, current head is {head_sha}."
            )
            continue
        approved.append(canonical)

    approved = sorted(set(approved), key=str.lower)
    print(
        f"Author {author} is not in {ORG}/{ENGINEERING_TEAM}. "
        f"Engineering approvals on {head_sha}: {len(approved)}/{REQUIRED}"
        + (f" ({', '.join(approved)})" if approved else " (none)")
    )

    if len(approved) < REQUIRED:
        missing = REQUIRED - len(approved)
        fail(
            f"Need {REQUIRED} distinct latest APPROVED reviews from active "
            f"{ORG}/{ENGINEERING_TEAM} members on the current head. "
            f"Have {len(approved)}; {missing} more required. After those "
            "approvals land, re-run this check (ruleset workflows do not "
            "run on pull_request_review)."
        )

    print("Required engineering approvals are present on the current head.")


if __name__ == "__main__":
    main()
