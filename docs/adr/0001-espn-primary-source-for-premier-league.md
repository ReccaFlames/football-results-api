# ESPN is the primary source for the Premier League

The free football-data.org plan returns Premier League fixtures and scores but no goal scorers
or bookings, and fouls and corners need a paid add-on, so cards, fouls, corners, and the first goal
were always `null`. ESPN's public scoreboard JSON supplies all of them for every fixture of a
season, free and without a token, so we made it the Premier League's primary source rather than
paying for football-data.org or adding ESPN only to enrich football-data.org matches.

## Considered options

- **Pay football-data.org** (about €44/month for cards, goals, fouls, and corners): rejected so
  that self-hosting stays free.
- **Enrich football-data.org matches from ESPN**: rejected as two sources, a per-match merge, and a
  separate store where one source already has everything.
- **football-data.co.uk CSVs / openfootball**: no goal minutes, or not updated for the current
  season.

## Consequences

- ESPN's API is undocumented and its terms allow personal, non-commercial use only; it may change
  without notice. football-data.org stays in the code as the planned fallback source (roadmap 17b).
- ESPN has no matchweek, so the stored round of already imported matches is kept and new fixtures
  have no round.
- Switching provider must keep match IDs: matches are matched by season, home team, and away team
  when the provider ID and round do not match, and configured team aliases keep the existing team
  names and slugs.
