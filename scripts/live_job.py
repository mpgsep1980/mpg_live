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
    get_division_team_names,
)
from core.live_scoring import compute_division_live_scores, collect_real_match_ids, FINISHED_MATCH_PERIODS
from core.archive import archive_closed_gameweek_if_needed
from core.live_projection import (
    league_setup, resolve_division_rows, resolve_league_wide_ranks, finalize_division_data, compute_super_classement,
    refresh_division_classement_from_archive,
)
from core.league import current_real_season_start_year, get_match_bonus_config


def supabase_client() -> Client:
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


def get_all_leagues(sb: Client) -> list[dict]:
    """Remplace core.league.get_all_leagues() (qui lit League_Codes.json en
    local cote mpg_app) -- meme forme de dict en sortie (memes cles
    camelCase : nom/code/seasonSearch/scoring/...) que celle attendue par
    core/league.py et core/live_projection.py, portes depuis mpg_app."""
    rows = sb.table("leagues").select("*").execute().data
    return [
        {
            "nom": r["nom"],
            "code": r["code"],
            "seasonSearch": r["season_search"],
            "seasonStart": r["season_start"],
            "championshipId": r["championship_id"],
            "playersNumber": r["players_number"],
            "playersPerDivision": r["players_per_division"],
            "poolGameweeks": r["pool_gameweeks"],
            "Div_A_Gameweeks": r["div_a_gameweeks"],
            "scoring": r.get("scoring") or {},
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
    local. Ecrit aussi division_classement_live (classement deja resolu --
    rang/badges/bonus internes, cf. core/live_projection.py) division par
    division, pour que site/division.html n'ait plus qu'a lire une ligne."""
    short_id = league["code"]
    season = league["seasonSearch"]
    total_divisions = get_live_total_divisions(short_id)

    division_matches_by_div = {}
    all_match_ids: set[str] = set()
    for division in range(1, total_divisions + 1):
        division_matches_by_div[division] = get_division_matches(short_id, season, division, game_week)
        all_match_ids |= collect_real_match_ids(division_matches_by_div[division])

    real_matches_by_id = {mid: get_championship_match(mid) for mid in all_match_ids}

    results_by_div = {}
    for division, division_matches in division_matches_by_div.items():
        results = compute_division_live_scores(division_matches, real_matches_by_id)
        results_by_div[division] = results
        sb.table("live_snapshots").upsert({
            "league_code": short_id,
            "season": season,
            "game_week": game_week,
            "division": division,
            "data": results,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).execute()

    print(f"  {league['nom']} : {total_divisions} division(s) ecrites pour J{game_week}.")

    # Classement de division deja resolu (rang/badges/Pichichi/Le_Mur/Boss) --
    # une passe par division (resolve_division_rows), puis une passe croisee
    # toutes divisions confondues pour rang_ligue/points_ligue (retour
    # utilisateur 2026-08-21, plan "Page Division pour mpg_live").
    setup = league_setup(sb, league, list(range(1, total_divisions + 1)), game_week)
    rows_by_division = {}
    for division, division_matches in results_by_div.items():
        rows, _is_live = resolve_division_rows(league, division, division_matches, game_week, total_divisions, setup)
        rows_by_division[division] = rows
    league_ranks = resolve_league_wide_ranks(rows_by_division, setup["match_bonus_cfg"])

    now = datetime.now(timezone.utc).isoformat()
    for division, rows in rows_by_division.items():
        try:
            team_names = get_division_team_names(short_id, season, division)
        except Exception:
            team_names = {}
        data = finalize_division_data(rows, league_ranks, team_names)
        # "X/N matchs reels" (retour utilisateur 2026-08-24, inspire du rendu
        # d'Ilan "8/10 matchs reels") -- quasi gratuit ici, real_matches_by_id
        # est deja entierement charge pour TOUTE la ligue plus haut (ligne
        # 109), on ne fait que compter par division.
        real_match_ids = collect_real_match_ids(division_matches_by_div[division])
        real_matches_done = sum(
            1 for mid in real_match_ids if real_matches_by_id.get(mid, {}).get("period") in FINISHED_MATCH_PERIODS
        )
        sb.table("division_classement_live").upsert({
            "league_code": short_id, "season": season, "division": division,
            "game_week": game_week, "data": data, "is_live": True, "updated_at": now,
            "real_matches_progress": {"done": real_matches_done, "total": len(real_match_ids)},
        }).execute()


def refresh_league_from_archive(sb, league: dict, total_divisions: int) -> None:
    """Rafraichit division_classement_live pour TOUTE une ligue depuis
    league_classement_archive seule (pas de poll live ce tick) -- pour
    qu'une division deja archivee (backfill, ou fenetre fermee sans poll
    live depuis) ne reste pas vide sur site/division.html tant qu'aucun
    poll live n'a eu lieu (retour utilisateur 2026-08-24 : "Aucun
    classement" affiche pour Ligue_Camembert alors que sa journee 1 etait
    deja archivee -- jamais rafraichi faute de fenetre live). N'ecrit rien
    pour une division sans aucune archive (saison pas encore commencee)."""
    short_id = league["code"]
    season = league["seasonSearch"]

    rows_by_division = {}
    last_game_week_by_division = {}
    for division in range(1, total_divisions + 1):
        rows, last_game_week = refresh_division_classement_from_archive(sb, league, division)
        if rows:
            rows_by_division[division] = rows
            last_game_week_by_division[division] = last_game_week
    if not rows_by_division:
        return

    match_bonus_cfg = get_match_bonus_config(league)
    league_ranks = resolve_league_wide_ranks(rows_by_division, match_bonus_cfg)

    now = datetime.now(timezone.utc).isoformat()
    for division, rows in rows_by_division.items():
        try:
            team_names = get_division_team_names(short_id, season, division)
        except Exception:
            team_names = {}
        data = finalize_division_data(rows, league_ranks, team_names)
        sb.table("division_classement_live").upsert({
            "league_code": short_id, "season": season, "division": division,
            "game_week": last_game_week_by_division[division], "data": data,
            "is_live": False, "updated_at": now,
        }).execute()


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
            # Archivage de la journee PRECEDENTE, independant de la journee
            # EN COURS calculee plus bas -- doit se faire meme si aucune
            # nouvelle fenetre n'est encore ouverte/connue. Idempotent
            # (is_gameweek_archived), donc verifie a CHAQUE tick sans risque
            # de double-compte -- retour utilisateur 2026-08-23 : c'est cette
            # meme discipline (sonde de sante a chaque tick plutot qu'un
            # declenchement one-shot) qui a corrige le meme genre de bug
            # cote mpg_app (live_scheduler.py).
            prev_state_res = sb.table("gameweek_state").select("*").eq("league_code", short_id).execute()
            if prev_state_res.data:
                prev_state = prev_state_res.data[0]
                if now > parse_iso(prev_state["window_end"]):
                    total_divisions = get_live_total_divisions(short_id)
                    newly_archived = archive_closed_gameweek_if_needed(sb, league, prev_state["game_week"], total_divisions)
                    if newly_archived:
                        # Rafraichit tout de suite division_classement_live avec
                        # le resultat fraichement archive -- sinon la page reste
                        # figee sur le dernier etat live (en cours) jusqu'a la
                        # PROCHAINE fenetre, retour utilisateur 2026-08-24.
                        refresh_league_from_archive(sb, league, total_divisions)

            # Rattrapage ponctuel : une ligue qui a une archive mais n'a
            # JAMAIS ete pollee en direct (backfill seul, cf. retour
            # utilisateur 2026-08-24 -- Ligue_Camembert fraichement ajoutee)
            # ne recevrait sinon jamais de ligne dans division_classement_live
            # avant sa prochaine fenetre live. Verifie une seule fois --
            # des qu'une ligne existe, plus jamais redeclenche par cette
            # branche (evite de refaire cet appel a chaque tick pour rien).
            has_live_row = (
                sb.table("division_classement_live").select("division")
                .eq("league_code", short_id).eq("season", league["seasonSearch"]).limit(1)
                .execute().data
            )
            if not has_live_row:
                total_divisions = get_live_total_divisions(short_id)
                refresh_league_from_archive(sb, league, total_divisions)

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
                # Rattrapage : une ligue qui a RATE TOUTE la fenetre live de la
                # journee precedente (cron en panne, ligue ajoutee en retard --
                # meme incident deja documente dans scripts/backfill_gameweek.py,
                # 2026-08-23 sur Ligue_2_EKT) n'a jamais de ligne dans
                # gameweek_state pour cette journee -- la branche d'archivage
                # normale (ligne 222 ci-dessus, gatee sur prev_state_res.data)
                # ne se declenche donc JAMAIS pour elle, meme une fois MPG
                # stabilise. Tente ici l'archivage direct de J(game_week-1),
                # idempotent (archive_closed_gameweek_if_needed se sait deja
                # sans effet si deja fait ou si MPG n'a pas encore stabilise) --
                # retour utilisateur 2026-08-24, decouvert sur Lega_Calzone/
                # Rosbeef_League (jamais aucune ligne dans division_classement_
                # live malgre une journee 1 en principe terminee).
                if not has_live_row and game_week > 1:
                    total_divisions = get_live_total_divisions(short_id)
                    archived = archive_closed_gameweek_if_needed(sb, league, game_week - 1, total_divisions)
                    if archived == total_divisions:
                        refresh_league_from_archive(sb, league, total_divisions)
                        print(f"  {name} : rattrapage J{game_week - 1} archivee ({archived}/{total_divisions}).")
                    elif archived:
                        print(f"  {name} : rattrapage J{game_week - 1} partiel ({archived}/{total_divisions}), reessai au prochain tick.")
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

    # Super Classement croise toutes ligues -- UNE fois par tick (pas par
    # ligue, cf. sa docstring : fusionne tout division_classement_live
    # d'un coup). Propre try/except, meme principe que chaque ligue
    # ci-dessus -- ne doit jamais faire echouer le reste du tick.
    try:
        season = current_real_season_start_year()
        ranked = compute_super_classement(sb)
        now_iso = datetime.now(timezone.utc).isoformat()
        for row in ranked:
            # super_classement n'a pas de colonne teamName dediee (cf.
            # db/schema.sql) -- range dans le meme champ jsonb que les
            # VRAIS bonus (bonus_details), sous une cle "teamName" qui ne
            # collisionne avec aucun nom de categorie (cf.
            # core/general_bonus.py::DEFAULT_GENERAL_BONUS_CATEGORIES).
            bonus_details = dict(row.get("bonus_details") or {})
            bonus_details["teamName"] = row.get("teamName", "")
            sb.table("super_classement").upsert({
                "season": season, "user_id": row["userId"], "points": row["points"],
                "bonus_details": bonus_details, "raw_stats": row.get("raw_stats") or {}, "updated_at": now_iso,
            }).execute()
        print(f"  Super Classement : {len(ranked)} manager(s) ecrits pour la saison {season}.")
    except Exception as e:
        print(f"  Super Classement : erreur inattendue ce tick ({e}) -- reessai au prochain cron.")


if __name__ == "__main__":
    main()
