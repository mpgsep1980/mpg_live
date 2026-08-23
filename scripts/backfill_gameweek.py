"""
Backfill ponctuel : archive une ou plusieurs journees DEJA TERMINEES d'une
ligue que le cron n'a jamais pollees en direct (retour utilisateur
2026-08-23 : le cron GitHub Actions a longtemps echoue silencieusement --
secrets mal configures -- pendant que Ligue_2_EKT jouait 2 journees
entierement suivies en local via mpg_app ; ces 2 journees n'existent nulle
part dans Supabase). Pas execute par le cron -- a lancer a la main, une fois
par journee/ligue manquante, une fois le cron confirme fonctionnel.

Meme decoupage "2 passes" que core/archive_capture.py (retour utilisateur
2026-08-23, "reconstruire le json de la journee ... eviter de multiples
appels API MPG") : pass 1 capture le JSON deja finalise par MPG (1 appel
get_division_matches + 1 get_division_calendar par division/journee), pass 2
(core/archive.py::archive_closed_gameweek_if_needed, INCHANGE, reutilise tel
quel) calcule points/classement dessus sans aucun autre appel API. Le JSON
capture est ecrit dans live_snapshots (meme table/forme que ce que le cron
ecrit en live, cf. scripts/live_job.py::poll_league) : archive_closed_
gameweek_if_needed n'a alors besoin d'aucune adaptation -- PK (league_code,
season, game_week, division) exclut tout conflit avec les journees courantes
que le cron ecrira plus tard.

IMPORTANT : traiter les journees dans l'ordre croissant (J1 avant J2 avant
J3...) -- l'archive cumule sur la base existante (league_classement_archive),
un ordre partiel ou inverse corromprait les totaux (victoires/buts/bonus).

Usage :
    python scripts/backfill_gameweek.py Ligue_2_EKT 1 2
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.live_job import supabase_client, get_all_leagues, get_live_total_divisions
from core.archive_capture import capture_division_journee
from core.archive import archive_closed_gameweek_if_needed


def backfill_gameweek(sb, league: dict, game_week: int, total_divisions: int) -> bool:
    """Capture + archive une journee pour TOUTES les divisions d'une ligue.
    Renvoie False (et n'archive rien) si au moins une division n'est pas
    encore stabilisee cote MPG -- ne jamais archiver une journee capturee
    partiellement (fausserait la base pour le reste de la saison)."""
    short_id = league["code"]
    season = league["seasonSearch"]

    captures = {}
    for division in range(1, total_divisions + 1):
        capture = capture_division_journee(short_id, season, division, game_week)
        if capture is None:
            print(f"    division {division} : pas encore stabilisee cote MPG, journee {game_week} reportee.")
            return False
        captures[division] = capture

    now = datetime.now(timezone.utc).isoformat()
    for division, capture in captures.items():
        sb.table("live_snapshots").upsert({
            "league_code": short_id, "season": season, "game_week": game_week,
            "division": division, "data": capture["divisionMatches"], "updated_at": now,
        }).execute()

    archive_closed_gameweek_if_needed(sb, league, game_week, total_divisions)
    print(f"    journee {game_week} : {total_divisions} division(s) capturees et archivees.")
    return True


def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        raise SystemExit(1)

    league_name = sys.argv[1]
    game_weeks = sorted(int(g) for g in sys.argv[2:])

    sb = supabase_client()
    leagues = get_all_leagues(sb)
    league = next((l for l in leagues if l["nom"] == league_name), None)
    if not league:
        raise SystemExit(f"Ligue inconnue en base Supabase : {league_name}")

    total_divisions = get_live_total_divisions(league["code"])
    print(f"{league_name} -- {total_divisions} division(s), journee(s) {game_weeks}")

    for game_week in game_weeks:
        print(f"  journee {game_week}...")
        backfill_gameweek(sb, league, game_week, total_divisions)


if __name__ == "__main__":
    main()
