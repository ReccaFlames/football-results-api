# Football Results API

Imports football fixtures and results from external sources and serves them to people and agents.

## Language

### Data freshness

**Stale season**:
A season that is refreshed automatically and was either never fetched or whose next scheduled
update is more than the grace period overdue.
_Avoid_: stale data, outdated season

**Stalled scheduler**:
The state in which no scheduler has sent a heartbeat within the allowed window, so no season is
being refreshed.
_Avoid_: stale scheduler, dead scheduler

**Grace period**:
How long a season's scheduled update may be late before the season counts as a stale season.

### Sources

**Primary source**:
The source that defines a season's matches: which fixtures exist, their teams, kickoff times,
status, and scores.
_Avoid_: main provider, master source

**Fallback source**:
A second source for a season, used for a single refresh only when the primary source has failed
several times in a row. It updates only what it supplies (fixtures, kickoff times, status, and
scores) and never erases statistics it cannot supply. The primary source is always tried first.
_Avoid_: backup provider

**Windowed source**:
A source that supplies only the matches around today, not a whole season, so it can serve only as
a fallback source, never as a primary source.
_Avoid_: partial provider, fallback-only provider

**Skipped match**:
A match that a fallback source supplies but a fallback refresh leaves out, because its home or
away team, after team aliases, is not yet a team of the season. It is logged and counted on the
import job, never stored.
_Avoid_: dropped match, ignored match

**Primary failure**:
A refresh in which the primary source errors, or returns no matches for a season that already has
matches.
_Avoid_: outage, provider down

### Match statistics

**Card count**:
The number of yellow cards plus red cards a team received in a match; a second yellow card counts
as one yellow card and one red card.
_Avoid_: bookings

**First goal**:
The earliest goal of a match, described by the team that scored it and its minute. An own goal
counts for the team whose score it raised. A goalless match has no first goal, which looks the same
as an unknown first goal; the score tells them apart.
_Avoid_: opening goal, first scorer

### Access

**Keyed client**:
A caller identified by a named API key (or a mapped token subject) whose request quotas are its
own; every other caller is an anonymous client, counted by its IP address.
_Avoid_: user, account, tenant

**API key role**:
What a keyed client may do: a `client` may only read, like an anonymous client; an `admin` may
also queue refreshes and historical fetches, follow jobs, and read operational data.
_Avoid_: permission level, scope

**Demo instance**:
A public, read-only deployment that serves only competitions whose sources permit public
redistribution.
_Avoid_: sandbox, public instance

**Private instance**:
An invite-only deployment in which every caller must be a keyed client. It may serve any
provider, but a provider that is not a redistributable provider may reach only keyed clients
covered by that provider's permission.
_Avoid_: production instance, closed instance

**Access request**:
A request by a person or an agent to become a keyed client of a private instance, with a contact
address and the intended use. It never grants access by itself: the operator approves or rejects
it, and only approval leads to an API key. Requests not approved are deleted after 30 days.
_Avoid_: signup, registration, whitelist entry

**Landing page**:
The human front page of an instance, on the same origin as the API, that introduces the API, lists
its competitions, links to the documentation, and (on a private instance) accepts access requests.
_Avoid_: home page, index, portal

**Redistributable provider**:
A data provider whose terms permit public redistribution of its data through this API; only these
may be served by a demo instance. The list (`REDISTRIBUTABLE_PROVIDERS`, today only OpenLigaDB)
is code, and adding one needs written permission.
_Avoid_: open provider, free provider
