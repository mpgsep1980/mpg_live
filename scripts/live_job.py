"""
Un seul tick idempotent, destine a etre rappele par un cron GitHub Actions
toutes les 5-10 minutes (cf. .github/workflows/live_job.yml) : consulte le
calendrier reel MPG (core.api.get_nearest_game_weeks), et pour chaque ligue
dont la fenetre de journee est ouverte (startDate <= now <= endDate -- une
journee peut s'etaler sur 4-5 jours reels, retour utilisateur 2026-08-14),
fait un poll (equivalent d'un tour de live_watch.py) et ecrit le resultat
dans Supabase.

Remplace le duo local live_scheduler.py (boucle infinie, poll calendrier) +
live_watch.py (boucle infinie, poll 60s) de mpg_app : ici pas de process a
faire survivre plusieurs jours d'affilee, juste des runs courts et repetes
par le cron GitHub Actions -- la repetition est geree par GitHub, pas par une
boucle Python locale.

Variables d'environnement requises :
    MPG_TOKEN      -- token API MPG (cf. core/token.py)
    SUPABASE_URL
    SUPABASE_KEY   -- cle service_role (ecriture) -- jamais exposee au site,
                      qui utilise uniquement la cle anonyme.

Chaque ligue est traitee dans son propre try/except (meme lecon que
live_scheduler.py cote mpg_app) : une erreur sur une ligue ne doit pas
empecher les autres d'etre pollees ce tick.
"""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from supabase import create_client, Client

from core.api import (
    get_championship_ids,
    get_nearest_game_weeks,
    get_division_matches,
    get_championship_match,
    get_dashboard,
)
from core.live_scoring import compute_division_live_scores, collect_real_match_ids


def supabase_client() -> Client:
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


def get_all_leagues(sb: Client) -> list[dict]:
    """Remplace core.league.get_all_leagues() (qui lit League_Codes.json en
    local cote mpg_app) -- meme forme de dict en sortie (nom, code,
    seasonSearch) que celle attendue par le reste du code."""
    rows = sb.table("leagues").select("*").execute().data
    return [
        {
            "nom": r["nom"],
            "code": r["code"],
            "seasonSearch": r["season_search"],
            "championshipId": r["championship_id"],
        }
        for r in rows
    ]


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def get_live_total_divisions(short_id: str) -> int:
    """totalDivisions REEL cote /dashboard -- meme raison que cote mpg_app
    (peut diverger d'un calcul local a partir de playersPerDivision)."""
    for tile in get_dashboard().get("orderedTiles", []):
        if tile.get("shortId") == short_id and tile.get("totalDivisions"):
            return tile["totalDivisions"]
    raise RuntimeError(f"totalDivisions introuvable pour {short_id} sur /dashboard.")


def poll_league(sb: Client, league: dict, game_week: int) -> None:
    """Un tour de poll pour toute la ligue -- meme logique que
    live_watch.py::fetch_league_live_snapshot cote mpg_app (mutualise les
    matchs reels entre divisions), ecrit dans Supabase au lieu d'un fichier
    local."""
    short_id = league["code"]
    season = league["seasonSearch"]
    total_divisions = get_live_total_divisions(short_id)

    division_matches_by_div = {}
    all_match_ids: set[str] = set()
    for division in range(1, total_divisions + 1):
        division_matches_by_div[division] = get_division_matches(short_id, season, division, game_week)
        all_match_ids |= collect_real_match_ids(division_matches_by_div[division])

    real_matches_by_id = {mid: get_championship_match(mid) for mid in all_match_ids}

    for division, division_matches in division_matches_by_div.items():
        results = compute_division_live_scores(division_matches, real_matches_by_id)
        sb.table("live_snapshots").upsert({
            "league_code": short_id,
            "season": season,
            "game_week": game_week,
            "division": division,
            "data": results,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).execute()

    print(f"  {league['nom']} : {total_divisions} division(s) ecrites pour J{game_week}.")


def main() -> None:
    sb = supabase_client()

    leagues = get_all_leagues(sb)
    if not leagues:
        raise SystemExit("Aucune ligue en base Supabase (table 'leagues' vide) -- rien a faire.")

    championship_ids = get_championship_ids()
    calendars = get_nearest_game_weeks()
    now = datetime.now(timezone.utc)

    print(f"[{datetime.now():%H:%M:%S}] Tick -- {len(leagues)} ligue(s) en base.")

    for league in leagues:
        name, short_id = league["nom"], league["code"]
        try:
            champ_id = championship_ids.get(short_id)
            if champ_id is None:
                print(f"  {name} : championshipId introuvable sur le dashboard, ignoree ce tick.")
                continue

            next_gw = calendars.get(str(champ_id), {}).get("nextGameWeek")
            if not next_gw or not next_gw.get("startDate") or not next_gw.get("endDate") or not next_gw.get("gameWeekNumber"):
                print(f"  {name} : pas de prochaine journee dans le calendrier.")
                continue

            window_start = parse_iso(next_gw["startDate"])
            window_end = parse_iso(next_gw["endDate"])
            game_week = next_gw["gameWeekNumber"]

            if not (window_start <= now <= window_end):
                print(f"  {name} : hors fenetre J{game_week} "
                      f"({window_start:%d/%m %H:%M} -> {window_end:%d/%m %H:%M} UTC).")
                continue

            print(f"  {name} : J{game_week} en cours, poll...")
            poll_league(sb, league, game_week)

            sb.table("gameweek_state").upsert({
                "league_code": short_id,
                "game_week": game_week,
                "window_start": window_start.isoformat(),
                "window_end": window_end.isoformat(),
                "last_polled_at": datetime.now(timezone.utc).isoformat(),
            }).execute()
        except Exception as e:
            print(f"  {name} : erreur inattendue ce tick ({e}) -- reessai au prochain cron.")


if __name__ == "__main__":
    main()
