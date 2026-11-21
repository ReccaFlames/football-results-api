# Fallback sources fill gaps for one refresh and never replace the primary

ESPN, the Premier League's primary source, is undocumented and may break without notice
([ADR 0001](0001-espn-primary-source-for-premier-league.md)). football-data.org's free plan has
fixtures, status, kickoff times, and scores, but no cards, fouls, corners, or goals. We use it as a
fallback source **per attempt**: after `fallback_after_failures` primary failures in a row (default
3), each scheduled check tries the primary first and, only if that fails, refreshes from the
fallback in the same run. A primary failure includes an empty result for a season that already has
matches.

A fallback refresh is a merge, not a reconciliation. It overwrites fixture fields (teams, kickoff,
status, full-time and half-time score), keeps stored statistics where it has none, may add matches
but never deletes them, and never writes provider match IDs or per-match source URLs.

**Amendment (roadmap 17c):** a fallback refresh skips a match whose home or away team, after
`team_aliases`, is not already a team of that season. It logs a warning and counts the match in
`scrape_run.matches_skipped`. A missing alias (for example for a newly promoted team) would
otherwise create a duplicate team and a duplicate match that only a primary refresh could remove.
A fallback refresh also calculates the next check from the season's stored matches after the merge,
so a windowed source ([ADR 0003](0003-thesportsdb-windowed-fallback-for-polish-competitions.md))
does not make the scheduler idle.

## Considered options

- **Sticky switch** (the season moves to the fallback until a person or a probe switches it back):
  rejected because it needs extra state and lets a free-plan source wipe statistics for the rest
  of the outage.
- **Fallback as full replacement** (a normal refresh with the other provider): rejected because it
  would set cards, fouls, corners, and first goals to `null`, delete matches the fallback lists
  differently, and replace the primary's match IDs.
- **Pay for football-data.org**: rejected in ADR 0001 to keep self-hosting free.

## Consequences

- Only a primary success resets the primary-failure count, so during an outage every check falls
  back at once instead of waiting for the threshold again.
- Matches that finish during an outage have `null` statistics until the primary recovers.
- `freshness.source` reports the source of the last successful import, which may be the fallback.
  The scheduler state stays the normal adaptive one, and metrics and alerts flag fallback use.
- Manual refreshes and historical seasons use the primary source only.
