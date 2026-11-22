# OpenLigaDB is the primary source for the 3. Liga

No current provider covers Germany's 3. Liga. ESPN's league index has `ger.1` and `ger.2` but no
`ger.3`, football-data.org offers the 3. Liga only on paid plans, and 90minut.pl covers only
Polish football and the European cups. OpenLigaDB has a documented JSON API
([OpenLigaDB-Samples](https://github.com/OpenLigaDB/OpenLigaDB-Samples), Swagger at
`https://api.openligadb.de`) with no key. One request, `getmatchdata/bl3/<start year>`, returns
the whole season with UTC kickoff times, matchdays, full-time and half-time scores, and goals with
minutes. We add an `openligadb` provider and make it the 3. Liga's **primary source**, with no
fallback source.

## Considered options

- **football-data.org paid plan** (3. Bundesliga): rejected to keep self-hosting free (ADR 0001).
- **ESPN**: has no 3. Liga.
- **TheSportsDB**: a windowed source only (ADR 0003), so it cannot be a primary source, and its
  free key does not list a 3. Liga that we could verify.
- **openfootball (CC0)**: its 2026-27 German data has only the Bundesliga.
- **Sportmonks free plan**: covers only the Danish Superliga and Scottish Premiership, needs a
  token, and its documentation does not confirm redistribution rights on the free plan.
- **Undocumented sites (DFB, kicker, FotMob)**: rejected because new providers must use documented
  or explicitly permitted access.

## Consequences

- OpenLigaDB data is licensed under the Open Database License (ODbL). The README,
  `PROVIDERS.md`, `/llms.txt`, and `/agents` attribute OpenLigaDB for competitions that use it.
  Operators who publish the data must keep that attribution, and a database derived from it is
  subject to the ODbL's share-alike terms.
- The data is community-edited: results appear when a user enters them, and errors are fixed by
  hand (for example, day and month swapped on some 2026-27 Frauen-Bundesliga matchdays, issue
  #135).
- OpenLigaDB has no status field. A postponed match keeps its old kickoff until a league admin
  moves it (issue #146, 3. Liga 2026-27), so it would look overdue forever. An unfinished match
  more than `postponed_after_hours` (default 24) past kickoff is therefore `inactive`. A result
  entered later than that is picked up at the next scheduled check, not by result polling.
- The free source has no cards, corners, or fouls; they stay `null`. The first goal is filled only
  when the goal list adds up to the full-time score.
- OpenLigaDB documents no rate limit. One request per refresh, throttled by
  `providers.openligadb.min_request_interval_seconds` (default 2 s), keeps the load low.
- 3. Liga team names come from OpenLigaDB, so no `team_aliases` are needed. A later fallback
  source would need them.
