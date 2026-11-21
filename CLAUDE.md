# Buildathon team repo

Two-person team (Ryan + teammate) competing in the Builders Buildathon. Final cutoff: **Sun Nov 22, 11:00 AM EST**. One track, one number, winner takes all; ties go to the earlier submission.

## Read first

1. `docs/00-overview.md`: event rules and timeline
2. `docs/01-tracks.md`: the four tracks, leaderboard snapshot, recommendation
3. `docs/decisions.md`: which track we chose and why (the source of truth)
4. `docs/log.md`: every experiment and submission so far
5. `docs/briefs/`: official track text and rule files (rules.md / protocol.md / SCORING.md / challenge.pdf win over any summary)

## Rules for agents working here

- `git pull` before starting. After any experiment or submission, add a row to `docs/log.md` (time, who, commit, change, local score, server score), then commit and push.
- Record decisions and abandoned approaches in `docs/decisions.md`, one line each.
- Measure with the local scorer before proposing a server submission; submission budgets are tiny (1-4/hour).
- Never commit data, raw inputs, credentials or tokens (see `.gitignore`). Submission repos have size limits (20-50 MB).
- Submitted commits must stay in the public repo until results are published: never force-push or rewrite history on main.
- No signatures or AI attribution in commit messages.
