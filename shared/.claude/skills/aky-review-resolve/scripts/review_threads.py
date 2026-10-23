#!/usr/bin/env python3
import argparse
import json
import subprocess
import sys

HUNK_TAIL = 4
AI_PREFIX = "🤖 "
EMPTY_MESSAGE = "No unresolved review comments found."

THREADS_QUERY = """
query($owner: String!, $repo: String!, $pr: Int!, $endCursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $pr) {
      reviewThreads(first: 100, after: $endCursor) {
        nodes {
          id
          isResolved
          isOutdated
          comments(first: 100) {
            nodes { body author { login } path line diffHunk }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""

REPLY_MUTATION = """
mutation($threadId: ID!, $body: String!) {
  addPullRequestReviewThreadReply(input: {pullRequestReviewThreadId: $threadId, body: $body}) {
    comment { id }
  }
}
"""

RESOLVE_MUTATION = """
mutation($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) { thread { id } }
}
"""


def list_threads() -> None:
    pr = gh("pr", "view", "--json", "number", "-q", ".number").strip()
    pages = json.loads(
        graphql(THREADS_QUERY, "--paginate", "--slurp", "-F", f"pr={pr}",
                "-F", "owner={owner}", "-F", "repo={repo}")
    )
    rows = []
    for page in pages:
        pull = page["data"]["repository"]["pullRequest"]
        if pull is None:
            sys.exit(f"PR #{pr} not found or not accessible")
        for thread in pull["reviewThreads"]["nodes"]:
            comments = thread["comments"]["nodes"]
            if thread["isResolved"] or not comments:
                continue
            first = comments[0]
            rows.append(json.dumps({
                "id": thread["id"],
                "path": first["path"],
                "line": first["line"],
                "isOutdated": thread["isOutdated"],
                "hunk": "\n".join(first["diffHunk"].splitlines()[-HUNK_TAIL:]),
                "comments": [
                    {"author": (c["author"] or {}).get("login", "unknown"), "body": c["body"]}
                    for c in comments
                ],
            }, ensure_ascii=False))
    print("\n".join(rows) if rows else EMPTY_MESSAGE)


def resolve_thread(thread_id: str, reply: str) -> None:
    graphql(REPLY_MUTATION, "-f", f"threadId={thread_id}", "-f", f"body={AI_PREFIX}{reply}")
    graphql(RESOLVE_MUTATION, "-f", f"threadId={thread_id}")
    print(f"Resolved thread {thread_id}")


def main() -> None:
    parser = argparse.ArgumentParser(description="List or resolve PR review threads.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="Print unresolved threads of the current branch's PR as JSON lines")
    resolve = sub.add_parser("resolve", help="Reply to a thread, then resolve it")
    resolve.add_argument("thread_id")
    resolve.add_argument("--reply", required=True)
    args = parser.parse_args()
    if args.command == "list":
        list_threads()
    else:
        resolve_thread(args.thread_id, args.reply)


def gh(*args: str) -> str:
    try:
        result = subprocess.run(["gh", *args], capture_output=True, text=True)
    except FileNotFoundError:
        sys.exit("GitHub CLI (gh) is not installed or not in PATH. Install: https://cli.github.com/")
    if result.returncode != 0:
        sys.exit(result.stderr.strip() or f"gh {args[0]} failed")
    return result.stdout


def graphql(query: str, *args: str) -> str:
    return gh("api", "graphql", "-f", f"query={query}", *args)


if __name__ == "__main__":
    main()
