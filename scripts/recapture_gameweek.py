"""
Recapture + reconstruction d'une journee DEJA ARCHIVEE, pour integrer une
correction MPG survenue apres coup (changement de buteur, but reclasse CSC,
etc. -- MPG peut modifier des notes/buts jusqu'a ~48h apres un match, retour
utilisateur 2026-08-24 : "il faut avoir une possibilite d'aller rechercher
ces journees modifiees et d'adapter les json").

A l'inverse de core/archive.py::archive_closed_gameweek_if_needed (fusion
incrementale, adaptee a une PREMIERE archive), ce script RECONSTRUIT
integralement le cumul d'une division a partir de TOUT son historique connu
(core/archive.py::rebuild_division_archive) -- seul moyen sur de corriger une
journee sans double-compter celles archivees par-dessus depuis.

Etapes : (1) re-capture la journee visee (nouvel appel API MPG, ecrase la
ligne live_snapshots existante -- meme PK que le live/backfill) ; (2)
reconstruit tout le cumul de la division depuis live_snapshots (toutes les
journees finalisees connues, dans l'ordre) et remplace
league_classement_archive en consequence.

Usage :
    python scripts/recapture_gameweek.py Ligue_2_EKT 1 3
    (recapture la journee 3 de la division 1, puis reconstruit son cumul)

    python scripts/recapture_gameweek.py Ligue_2_EKT all 3
    (idem pour TOUTES les divisions -- plus lent, un appel API par division)
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.live_job import supabase_client, get_all_leagues, get_live_total_divisions
from core.archive_capture import capture_division_journee
from core.archive import rebuild_division_archive


def recapture_division_gameweek(sb, league: dict, division: int, game_week: int) -> bool:
    """Re-fetch MPG pour UNE division/journee et ecrase live_snapshots.
    Renvoie False (rien ecrase) si MPG n'a toujours pas de resultat final a
    ce game_week/division -- p.ex. rejoue trop tot apres un changement en
    cours de stabilisation."""
    short_id = league["code"]
    season = league["seasonSearch"]
    capture = capture_division_journee(short_id, season, division, game_week)
    if capture is None:
        print(f"    division {division} : toujours pas finalisee cote MPG pour J{game_week}, rien change.")
        return False
    sb.table("live_snapshots").upsert({
        "league_code": short_id, "season": season, "game_week": game_week,
        "division": division, "data": capture["divisionMatches"],
        "precious_holder_user_id": capture.get("preciousHolderUserId"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).execute()
    return True


def main() -> None:
    if len(sys.argv) != 4:
        print(__doc__)
        raise SystemExit(1)

    league_name, division_arg, game_week = sys.argv[1], sys.argv[2], int(sys.argv[3])

    sb = supabase_client()
    leagues = get_all_leagues(sb)
    league = next((l for l in leagues if l["nom"] == league_name), None)
    if not league:
        raise SystemExit(f"Ligue inconnue en base Supabase : {league_name}")

    total_divisions = get_live_total_divisions(league["code"])
    divisions = range(1, total_divisions + 1) if division_arg == "all" else [int(division_arg)]

    for division in divisions:
        print(f"  division {division} -- recapture J{game_week}...")
        if recapture_division_gameweek(sb, league, division, game_week):
            n = rebuild_division_archive(sb, league, division, total_divisions)
            print(f"    reconstruite : {n} journee(s) rejouee(s).")


if __name__ == "__main__":
    main()
