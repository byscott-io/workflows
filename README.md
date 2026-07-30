# workflows

Centralized reusable GitHub Actions workflows for the ByScott fleet.

Public so that **both public and private** fleet repos can call these reusables
(public repos can't call reusables from a private repo).

## Workflows
- `claude-pr-review.yml` — Claude auto-review on non-draft, non-Dependabot PRs.
- `claude-mention.yml` — interactive Claude on `@claude` comments.

**Edit the review prompt / skip rules here → every repo updates.** Callers are
thin (`uses: byscott-io/workflows/.github/workflows/<file>@master`). No secrets
live here; each caller passes `ANTHROPIC_API_KEY` + `CLAUDE_GH_TOKEN` at runtime.
