"""
Archivage de fin de journee -- cumule le resultat FINAL d'une journee dans
league_classement_archive, une fois sa fenetre (gameweek_state.window_end)
depassee. Appele depuis scripts/live_job.py, jamais par le site.

Idempotent par construction (is_gameweek_archived) : peut etre appele a
chaque tick sans jamais double-compter, meme si le scheduler tourne en
continu pendant plusieurs jours apres la fin reelle d'une journee.
"""
from datetime import datetime, timezone

from core.live_projection import (
    load_base_division_classement, is_gameweek_archived, division_delta, combined_division_standings,
)


def archive_closed_gameweek_if_needed(sb, league: dict, closed_game_week: int, total_divisions: int) -> None:
    """Archive la journee `closed_game_week` de `league`, division par
    division, si ce n'est pas deja fait (cf. is_gameweek_archived). Source
    des matchs : le DERNIER live_snapshots connu pour cette journee/division
    (deja au format compute_division_live_scores -- meme "score" que celui
    attendu par division_delta, aucune adaptation de champ necessaire).
    Rien a archiver (division jamais suivie ce jour-la, ou league_code
    absent de live_snapshots) -- ignore silencieusement cette division."""
    short_id = league["code"]
    season = league["seasonSearch"]

    for division in range(1, total_divisions + 1):
        base_by_user = load_base_division_classement(sb, short_id, season, division)

        snap_res = (
            sb.table("live_snapshots").select("data")
            .eq("league_code", short_id).eq("season", season)
            .eq("game_week", closed_game_week).eq("division", division)
            .execute()
        )
        if not snap_res.data:
            continue
        division_matches = snap_res.data[0]["data"]
        user_ids = [m[side].get("userId") for m in division_matches for side in ("home", "away")]

        if is_gameweek_archived(base_by_user, user_ids, closed_game_week):
            continue

        delta_rows = division_delta(division_matches, division, closed_game_week, league, total_divisions)
        combined = combined_division_standings(base_by_user, delta_rows)

        now = datetime.now(timezone.utc).isoformat()
        for row in combined:
            stats = {
                "teamName": row.get("teamName", ""),
                "victory": row["victory"], "draw": row["draw"], "defeat": row["defeat"],
                "matches_joues": row["matches_joues"],
                "score+": row["buts_pour"], "score-": row["buts_contre"],
                "points_pond": row["points_pond"],
                "cleanSheet": row["cleanSheet"], "manita": row["manita"], "on_fire": row["on_fire"],
                "grotaldo": row["grotaldo"], "owngoals": row["owngoals"],
            }
            sb.table("league_classement_archive").upsert({
                "league_code": short_id, "season": season, "division": division,
                "user_id": row["userId"], "stats": stats, "updated_at": now,
            }).execute()
