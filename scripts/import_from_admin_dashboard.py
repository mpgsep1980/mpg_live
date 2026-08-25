"""
Pont MANUEL : dashboard local (mpg_app/admin_server.py, port 5055,
alimente par mpg_app/live_watch.py) -> Supabase (mpg_live). Retour
utilisateur 2026-08-25 : "il me faut une page que tu sauras lire pour
alimenter mpg_live" -- filet de secours pour pousser des donnees a la
main si le poll automatique (scripts/live_job.py, cron GitHub Actions)
est en panne ou absent PENDANT les matchs.

Source : /api/live-snapshot (mpg_app/admin_server.py) -- deja calcule
par live_watch.py (compute_division_live_scores), aucune I/O MPG
supplementaire ici. Pousse dans Supabase via LES MEMES fonctions que
scripts/live_job.py::poll_league (resolve_division_rows /
resolve_league_wide_ranks / finalize_division_data) -- aucune logique
de calcul dupliquee ni reimplementee, seule la SOURCE des matchs change
(snapshot local relu au lieu d'un fetch MPG frais).

Prerequis avant de lancer :
- mpg_app/lancer_live.bat (ou `python admin_server.py`) tourne sur
  127.0.0.1:5055
- mpg_app/live_watch.py tourne pour la ligue/journee visee, ex. :
  python live_watch.py --ligue Rosbeef_League --gameweek 1 --interval 60
  (c'est LUI qui ecrit le fichier que /api/live-snapshot relit -- sans
  lui, /api/live-snapshot renvoie 404 "live_watch.py tourne-t-il ?")

Usage :
  python scripts/import_from_admin_dashboard.py --code QLJUL4VV4 --gameweek 1
  python scripts/import_from_admin_dashboard.py --all-tracked
    (toutes les ligues Supabase, journee courante par ligue -- cf.
    core.league.get_all_leagues -- saute silencieusement celles sans
    instantane local disponible)
"""
import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import requests
from dotenv import load_dotenv
from supabase import create_client

load_dotenv(Path(__file__).parent.parent / ".env")

from core.api import get_division_team_names
from core.live_projection import (
    league_setup, resolve_division_rows, resolve_league_wide_ranks, finalize_division_data,
)
from scripts.live_job import get_all_leagues, get_live_total_divisions

ADMIN_SERVER_BASE = os.environ.get("ADMIN_SERVER_BASE", "http://127.0.0.1:5055")


def supabase_client():
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


def fetch_local_snapshot(short_id: str, season: int, game_week: int) -> dict | None:
    """Relit /api/live-snapshot (mpg_app/admin_server.py). None si absent
    (live_watch.py pas lance pour cette ligue/journee, ou dashboard local
    pas demarre) -- jamais une exception, l'appelant decide de sauter ou
    d'alerter."""
    try:
        r = requests.get(
            f"{ADMIN_SERVER_BASE}/api/live-snapshot",
            params={"shortId": short_id, "season": season, "gameweek": game_week},
            timeout=10,
        )
    except requests.RequestException as e:
        print(f"  [{short_id}] dashboard local injoignable sur {ADMIN_SERVER_BASE} ({e}).")
        return None
    if r.status_code != 200:
        print(f"  [{short_id}] pas d'instantane local pour J{game_week} ({r.status_code}: {r.json().get('error', r.text)[:120]}).")
        return None
    return r.json()


def import_league(sb, league: dict, game_week: int) -> bool:
    """Pousse UNE ligue/journee depuis le dashboard local vers
    division_classement_live (+ live_snapshots en repli pour
    rebuild_division_archive). Renvoie True si quelque chose a ete
    ecrit."""
    short_id, season, name = league["code"], league["seasonSearch"], league["nom"]
    snapshot = fetch_local_snapshot(short_id, season, game_week)
    if not snapshot:
        return False

    divisions_data = snapshot.get("divisions", {})
    if not divisions_data:
        print(f"  [{name}] instantane local vide pour J{game_week}, rien a pousser.")
        return False

    total_divisions = get_live_total_divisions(short_id)
    results_by_div = {int(k): v for k, v in divisions_data.items()}

    now = datetime.now(timezone.utc).isoformat()

    setup = league_setup(sb, league, list(range(1, total_divisions + 1)), game_week)
    rows_by_division = {}
    is_live_by_division = {}
    for division, division_matches in results_by_div.items():
        rows, is_live = resolve_division_rows(league, division, division_matches, game_week, total_divisions, setup)
        rows_by_division[division] = rows
        is_live_by_division[division] = is_live
    league_ranks = resolve_league_wide_ranks(rows_by_division, setup["match_bonus_cfg"])

    # live_snapshots seulement pour les divisions PAS DEJA archivees (retour
    # utilisateur 2026-08-25, regression constatee et corrigee sur Ligue_2_EKT
    # J3) : une division deja archivee a une source plus fiable (capture_
    # division_journee, cf. core/archive.py) que l'instantane local relu ici
    # -- l'ecraser avec des donnees potentiellement plus anciennes degraderait
    # un futur rebuild_division_archive sans rien apporter (l'archive existe
    # deja et resolve_division_rows l'a de toute facon utilisee, pas
    # division_matches, pour calculer `rows` ci-dessus).
    for division, division_matches in results_by_div.items():
        if not is_live_by_division[division]:
            continue
        sb.table("live_snapshots").upsert({
            "league_code": short_id, "season": season, "game_week": game_week,
            "division": division, "data": division_matches, "updated_at": now,
        }).execute()

    for division, rows in rows_by_division.items():
        try:
            team_names = get_division_team_names(short_id, season, division)
        except Exception:
            team_names = {}
        data = finalize_division_data(rows, league_ranks, team_names)
        # is_live = ce que resolve_division_rows a lui-meme determine (via
        # is_gameweek_archived) -- PAS un True fige : si cette journee est
        # deja archivee cote Supabase, resolve_division_rows relit deja
        # l'archive et ignore le snapshot local passe en entree -- ecrire
        # is_live=True aurait ecrase a tort une journee correctement
        # archivee par le pipeline officiel (regression constatee et
        # corrigee 2026-08-25 sur Ligue_2_EKT J3).
        # real_matches_progress omis (retour utilisateur 2026-08-25) : la
        # forme du snapshot local (deja post-compute_division_live_scores,
        # sans playersOnPitch) est incompatible avec collect_real_match_ids
        # (attend des matchs MPG bruts) -- pas de valeur fiable a calculer
        # sans refaire un appel reseau, hors scope d'un pont manuel simple.
        sb.table("division_classement_live").upsert({
            "league_code": short_id, "season": season, "division": division,
            "game_week": game_week, "data": data, "is_live": is_live_by_division[division], "updated_at": now,
        }).execute()

    print(f"  [{name}] J{game_week} : {len(results_by_div)} division(s) poussee(s) depuis le dashboard local.")
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--code", help="Code court de la ligue (ex. QLJUL4VV4) -- avec --gameweek")
    parser.add_argument("--gameweek", type=int, help="Journee a pousser -- avec --code")
    parser.add_argument("--all-tracked", action="store_true", help="Toutes les ligues Supabase, journee courante calculee par ligue")
    args = parser.parse_args()

    sb = supabase_client()
    leagues = get_all_leagues(sb)

    if args.code:
        if not args.gameweek:
            parser.error("--code necessite --gameweek")
        league = next((l for l in leagues if l["code"] == args.code), None)
        if not league:
            parser.error(f"Ligue introuvable en base Supabase pour le code {args.code}")
        import_league(sb, league, args.gameweek)
        return

    if args.all_tracked:
        # Journee = celle deja connue de division_classement_live pour cette
        # ligue (dernier tick, live ou archive) -- pas de recalcul du vrai
        # calendrier MPG ici (cf. league_setup/scripts/live_job.py::main
        # pour cette logique complete, hors scope d'un pont manuel simple).
        # Une ligue jamais encore suivie par Supabase est juste ignoree :
        # utiliser --code/--gameweek explicitement pour un premier import.
        any_pushed = False
        for league in leagues:
            row = (
                sb.table("division_classement_live").select("game_week")
                .eq("league_code", league["code"]).limit(1).execute().data
            )
            if not row:
                print(f"  [{league['nom']}] jamais suivie par Supabase, ignoree (utilise --code/--gameweek).")
                continue
            any_pushed = import_league(sb, league, row[0]["game_week"]) or any_pushed
        if not any_pushed:
            print("Rien pousse -- verifie que admin_server.py et live_watch.py tournent bien.")
        return

    parser.error("Precise --code + --gameweek, ou --all-tracked")


if __name__ == "__main__":
    main()
