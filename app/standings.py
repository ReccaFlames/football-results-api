"""League table calculation from stored finished matches."""

from dataclasses import dataclass, field

from app.models import Match, StandingRow

POINTS_FOR_WIN = 3
POINTS_FOR_DRAW = 1
FORM_LENGTH = 5


@dataclass
class _Tally:
    slug: str
    name: str
    won: int = 0
    drawn: int = 0
    lost: int = 0
    goals_for: int = 0
    goals_against: int = 0
    form: list[str] = field(default_factory=list)

    @property
    def points(self) -> int:
        return self.won * POINTS_FOR_WIN + self.drawn * POINTS_FOR_DRAW


def _order_key(match: Match) -> tuple[float, int]:
    return (float("-inf") if match.kickoff_at is None else match.kickoff_at.timestamp(), match.id)


def compute_standings(matches: list[Match]) -> list[StandingRow]:
    """Rank teams by points, goal difference, goals for, wins, then team name.

    Only finished matches with both goal values count. Head-to-head records and point deductions
    are not applied.
    """
    tallies: dict[str, _Tally] = {}
    for match in sorted(matches, key=_order_key):
        if match.status != "finished" or match.home_goals is None or match.away_goals is None:
            continue
        sides = (
            (match.home_team_slug, match.home_team, match.home_goals, match.away_goals),
            (match.away_team_slug, match.away_team, match.away_goals, match.home_goals),
        )
        for slug, name, scored, conceded in sides:
            tally = tallies.setdefault(slug, _Tally(slug=slug, name=name))
            # Use the most recent display name the source gave this team.
            tally.name = name
            tally.goals_for += scored
            tally.goals_against += conceded
            if scored > conceded:
                tally.won += 1
                outcome = "W"
            elif scored == conceded:
                tally.drawn += 1
                outcome = "D"
            else:
                tally.lost += 1
                outcome = "L"
            tally.form.append(outcome)

    ranked = sorted(
        tallies.values(),
        key=lambda item: (
            -item.points,
            -(item.goals_for - item.goals_against),
            -item.goals_for,
            -item.won,
            item.name.casefold(),
            item.slug,
        ),
    )
    return [
        StandingRow(
            position=position,
            team_slug=item.slug,
            team_name=item.name,
            played=item.won + item.drawn + item.lost,
            won=item.won,
            drawn=item.drawn,
            lost=item.lost,
            goals_for=item.goals_for,
            goals_against=item.goals_against,
            goal_difference=item.goals_for - item.goals_against,
            points=item.points,
            form="".join(reversed(item.form[-FORM_LENGTH:])),
        )
        for position, item in enumerate(ranked, start=1)
    ]
