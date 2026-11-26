# Chat-client checklist (manual)

Chat clients connect to the **public demo** only (anonymous MCP; ADR 0007, roadmap item 20).
Run this after each release that changes MCP tools or the demo, and record the date and
result in the pull request. Client menus change often: if a step below no longer matches,
follow the client's current "custom connector" or "remote MCP server" guide and fix this file.

Demo MCP URL: `https://<demo host>/mcp` (no authentication).

## Connect

- [ ] **claude.ai**: Settings → Connectors → Add custom connector; name `Football Results`,
      URL the demo MCP URL; no OAuth fields. Enable it in a new chat (tools menu).
- [ ] **ChatGPT**: enable developer mode for connectors (Settings → Apps/Connectors →
      Advanced), create a connector with the demo MCP URL and no authentication, and add it
      to a new chat.
- [ ] Both clients list 5 tools: `list_competitions`, `find_matches`, `get_standings`,
      `get_team_form`, `request_refresh`.

## Ask (fresh chat for each, compare with `football-results` or the API)

- [ ] "Which competitions do you have football data for?" (uses `list_competitions`)
- [ ] "Who leads the 3. Liga right now?" (`get_standings`; leader and points match)
- [ ] "What were <second-placed team>'s last five results?" (`get_team_form`)
- [ ] "When do <team> play next?" (`find_matches`; date in a sensible time zone)
- [ ] "How many points does <team with a typo> have?" (recovers to the right team)
- [ ] "Who leads Serie A?" (says the service has no Serie A data; does not invent one)
- [ ] "How fresh is this data?" (quotes `data_as_of` / freshness, not a guess)
- [ ] "Refresh the 3. Liga" (`request_refresh` fails with `writes-disabled`, and the reply
      explains that the demo is read-only)
- [ ] The replies attribute OpenLigaDB (ODbL) data when asked where the data comes from

## Record

| Date | Client | Model | Connect | Questions passed | Notes |
| --- | --- | --- | --- | --- | --- |
| | claude.ai | | | /9 | |
| | ChatGPT | | | /9 | |
