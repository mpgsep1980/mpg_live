"""
Backfill ponctuel : archive une ou plusieurs journees DEJA TERMINEES d'une
ligue que le cron n'a jamais pollees en direct (retour utilisateur
2026-08-23 : le cron GitHub Actions a longtemps echoue silencieusement --
secrets mal configures -- pendant que Ligue_2_EKT jouait 2 journees
entierement suivies en local via mpg_app ; ces 2 journees n'existent nulle
part dans Supabase). Pas execute par le cron -- a lancer a la main, une fois
par journee/ligue manquante, une fois le cron confirme fonctionnel.

Simple fine couche CLI autour de core/archive.py::archive_closed_gameweek_if_
needed, qui capture elle-meme le JSON MPG-finalise (retour utilisateur
2026-08-23, "reconstruire le json de la journee ... eviter de multiples
appels API MPG" -- 1 appel get_division_matches + 1 get_division_calendar
par division/journee, cf. core/archive_capture.py) -- ce script n'a plus
qu'a boucler sur les journees demandees et rapporter si chacune est
complete.

IMPORTANT : traiter les journees dans l'ordre croissant (J1 avant J2 avant
J3...) -- l'archive cumule sur la base existante (league_classement_archive),
un ordre partiel ou inverse corromprait les totaux (victoires/buts/bonus).
Pour CORRIGER une journee DEJA archivee (resultat MPG modifie apres coup),
utiliser scripts/recapture_gameweek.py a la place -- pas celui-ci.

Usage :
    python scripts/backfill_gameweek.py Ligue_2_EKT 1 2
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.live_job import supabase_client, get_all_leagues, get_live_total_divisions
from core.archive import archive_closed_gameweek_if_needed


def backfill_gameweek(sb, league: dict, game_week: int, total_divisions: int) -> bool:
    """Archive une journee pour TOUTES les divisions d'une ligue. Renvoie
    False si au moins une division n'est pas encore stabilisee cote MPG
    (rien de perdu : les divisions deja pretes sont quand meme archivees,
    reessayer plus tard pour les autres -- idempotent)."""
    archived = archive_closed_gameweek_if_needed(sb, league, game_week, total_divisions)
    if archived < total_divisions:
        print(f"    journee {game_week} : {archived}/{total_divisions} division(s) archivees "
              f"-- le reste n'est pas encore stabilise cote MPG, a relancer plus tard.")
        return False
    print(f"    journee {game_week} : {total_divisions} division(s) archivees.")
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
