"""Publish a claude-code-action run's cost to the job summary, the review and metrics.

Reporting must never fail the job it reports on, so every destination is
independently guarded and the script always exits 0.
"""

import json
import os
import subprocess
import urllib.error
import urllib.request

# Marks a cost block already appended, so a re-run of this step does not stack a
# second table onto the same review.
MARKER = "<!-- claude-run-cost -->"


def write_summary(text):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a") as fh:
            fh.write(text + "\n")
    except OSError as exc:
        print(f"could not write job summary: {exc}")


def load_result(path):
    """The last `result` entry from the execution log, or None.

    The log is JSON-lines in some versions of the action and a JSON array in
    others. Accept either rather than assume.
    """
    if not path or not os.path.exists(path):
        return None

    with open(path) as fh:
        raw = fh.read().strip()

    entries = []
    try:
        parsed = json.loads(raw)
        entries = parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass

    results = [e for e in entries if isinstance(e, dict) and e.get("type") == "result"]
    return results[-1] if results else None


def number(value):
    return value if isinstance(value, (int, float)) else 0


def gh_api(args):
    """A gh api call that returns parsed JSON, or None on any failure."""
    try:
        out = subprocess.run(
            ["gh", "api"] + args, capture_output=True, text=True, timeout=30, check=True
        ).stdout
        return json.loads(out) if out.strip() else None
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as exc:
        print(f"gh api {' '.join(args)} failed: {exc}")
        return None


def post_comment(repo, issue_number, table):
    """Fall back to a comment when there is no review to append to.

    Needed for more than tidiness: an @claude mention on an ISSUE produces no
    review at all, so without this those runs report nothing anywhere.
    """
    if not (repo and issue_number and os.environ.get("GH_TOKEN")):
        print("no token/number context; cost not published to the thread")
        return

    body = f"{MARKER}\n{table}"
    try:
        subprocess.run(
            ["gh", "api", "--method", "POST",
             f"repos/{repo}/issues/{issue_number}/comments",
             "-f", f"body={body}"],
            capture_output=True, text=True, timeout=30, check=True,
        )
        print("posted cost as a comment")
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"could not post cost comment: {exc}")


def publish_to_thread(repo, pr_number, table):
    """Put the cost where the review is READ, not two clicks away in Actions.

    Appended to the review when this run submitted one, because the review body
    is written BY the Claude step, which cannot know what it cost until it has
    finished -- so the number goes on afterwards by updating the review rather
    than being built into the body. Otherwise posted as a comment.
    """
    if not (repo and pr_number and os.environ.get("GH_TOKEN")):
        print("no token/PR context; cost not published to the thread")
        return

    reviews = gh_api([f"repos/{repo}/pulls/{pr_number}/reviews", "--paginate"])
    bot_reviews = [
        r
        for r in (reviews if isinstance(reviews, list) else [])
        if isinstance(r, dict) and str((r.get("user") or {}).get("login", "")).endswith("[bot]")
    ]
    if not bot_reviews:
        print("no bot review to append to; falling back to a comment")
        post_comment(repo, pr_number, table)
        return

    # The newest bot review: this step runs immediately after it was submitted.
    review = max(bot_reviews, key=lambda r: number(r.get("id")))
    body = review.get("body") or ""
    if MARKER in body:
        print("review already carries a cost block; leaving it alone")
        return

    payload = {"body": f"{body}\n\n---\n{MARKER}\n{table}"}
    try:
        subprocess.run(
            [
                "gh",
                "api",
                "--method",
                "PUT",
                f"repos/{repo}/pulls/{pr_number}/reviews/{review['id']}",
                "--input",
                "-",
            ],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        print(f"appended cost to review {review['id']}")
    except (subprocess.SubprocessError, OSError) as exc:
        # An edit can be refused where a comment is not, so still report.
        print(f"could not append cost to review ({exc}); falling back to a comment")
        post_comment(repo, pr_number, table)


def record_in_metrics(result, tokens):
    key = os.environ.get("METRICS_KEY")
    if not key:
        print("no metricsbyscott key supplied; summary only, so no history")
        return

    payload = {
        "event": "claude_review.completed",
        "properties": {
            "repo": os.environ.get("REPO"),
            "pr_number": os.environ.get("PR_NUMBER"),
            "conclusion": os.environ.get("CLAUDE_CONCLUSION"),
            "cost_usd": result.get("total_cost_usd"),
            "num_turns": result.get("num_turns"),
            "duration_ms": result.get("duration_ms"),
            "run_url": os.environ.get("RUN_URL"),
            **tokens,
        },
    }
    req = urllib.request.Request(
        os.environ.get("METRICS_URL") or "https://metrics.byscott.io/api/events",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "X-Metrics-Api-Key": key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"recorded in metricsbyscott: HTTP {resp.status}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"could not record in metricsbyscott: {exc}")


def main():
    result = load_result(os.environ.get("EXECUTION_FILE"))
    if result is None:
        write_summary("### Claude run cost\n\nNo cost in the execution log, so it is unknown.")
        return

    usage = result.get("usage") or {}
    tokens = {
        "input_tokens": number(usage.get("input_tokens")),
        "output_tokens": number(usage.get("output_tokens")),
        "cache_read_input_tokens": number(usage.get("cache_read_input_tokens")),
        "cache_creation_input_tokens": number(usage.get("cache_creation_input_tokens")),
    }
    cost = result.get("total_cost_usd")
    turns = result.get("num_turns")
    ms = result.get("duration_ms")

    cost_str = f"${cost:.4f}" if isinstance(cost, (int, float)) else "unknown"
    seconds = round(ms / 1000.0, 1) if isinstance(ms, (int, float)) else "unknown"
    run_url = os.environ.get("RUN_URL") or ""

    # Cache reads are shown because they dominate: the input_tokens figure
    # excludes them, so on its own it reads as though the run saw almost no
    # input (one review reported 14 input tokens against 10,850 output).
    table = (
        f"**Review cost: {cost_str}** · {turns if turns is not None else '?'} turns · {seconds}s\n\n"
        f"| tokens | |\n|---|---|\n"
        f"| input | {tokens['input_tokens']:,} |\n"
        f"| output | {tokens['output_tokens']:,} |\n"
        f"| cache read | {tokens['cache_read_input_tokens']:,} |\n"
        f"| cache write | {tokens['cache_creation_input_tokens']:,} |\n"
    )
    if run_url:
        table += f"\n[run]({run_url})\n"

    write_summary(f"### Claude run cost\n\n{table}")
    print(
        f"run cost {cost_str}, {tokens['input_tokens']} in / "
        f"{tokens['output_tokens']} out, {turns} turns"
    )

    publish_to_thread(os.environ.get("REPO"), os.environ.get("PR_NUMBER"), table)
    record_in_metrics(result, tokens)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - reporting must never fail the job
        print(f"cost reporting failed: {exc}")
