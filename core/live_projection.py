"""
Projection "provisoire" du classement de division pour mpg_live -- combine
la base archivee (Supabase, table league_classement_archive) avec le delta
de la journee EN COURS (division_matches deja calcules par
scripts/live_job.py via core.live_scoring.compute_division_live_scores).

Version reduite du module du meme nom cote mpg_app : pas de fusion
multi-ligues, pas de bonus generaux/Multi Boss, pas de Precieux ni de
Grotaldo (aucune source Supabase pour l'un ou l'autre en v1 -- cf. plan
"Page Division pour mpg_live", Etape 3). Reutilise telles quelles
core.scoring.compute_matchday_standings/aggregate_standings et
core.internal_bonus.compute_internal_bonuses -- memes fonctions, meme
tiebreak de division deja corrige cote mpg_app, aucune reimplementation.

Alimente scripts/live_job.py, qui ecrit le resultat (deja entierement
resolu -- rang, badges, bonus) dans division_classement_live. Le site
statique (site/division.html) ne fait que lire cette table, aucune logique
de classement/bonus cote client.
"""
import time

from core.api import get_division_calendar
from core.league import get_scoring_config, get_internal_bonus_config, get_match_bonus_config
from core.scoring import compute_matchday_standings, aggregate_standings
from core.internal_bonus import compute_internal_bonuses

LIVE_COUNTER_KEYS = ("cleanSheet", "manita", "on_fire")

# Champs publics ecrits dans division_classement_live.data (cf. db/schema.sql)
# -- tout le reste (points_pond, _bonus_champion, _bonus_podium) est interne,
# utilise seulement par resolve_league_wide_ranks, jamais publie tel quel.
PUBLIC_ROW_FIELDS = (
    "userId", "teamName", "rang", "points", "matches_joues",
    "victoires", "nuls", "defaites", "buts_pour", "buts_contre", "diff",
    "cleanSheet", "manita", "on_fire", "pichichi", "mur", "boss",
)


def _raw_points(victory: float, draw: float) -> float:
    """Points bruts (non ponderes) V/N/D deja joues -- port de
    mpg_app::_raw_points."""
    return victory * 3 + draw * 1


def load_base_division_classement(sb, league_code: str, season: int, division: int) -> dict:
    """{userId: stats} -- remplace mpg_app::load_base_league_classement
    (lecture fichier) par une lecture Supabase league_classement_archive,
    deja filtree par division (contrairement a mpg_app, qui charge toute la
    ligue d'un coup -- inutile ici, resolve_division_rows traite une
    division a la fois). Vide si cette division n'a encore aucune journee
    archivee (saison tout juste demarree, ou table pas encore alimentee --
    cf. core/archive.py::archive_closed_gameweek_if_needed)."""
    rows = (
        sb.table("league_classement_archive")
        .select("user_id, stats")
        .eq("league_code", league_code).eq("season", season).eq("division", division)
        .execute().data
    )
    return {r["user_id"]: r["stats"] for r in rows}


def is_gameweek_archived(base_by_user: dict, user_ids: list[str], game_week: int) -> bool:
    """Port pur de mpg_app::_is_gameweek_archived -- True si l'archive
    couvre deja cette journee (ou une plus recente) : le recombiner avec le
    delta live ferait double-compte victoires/buts/bonus."""
    for uid in user_ids:
        base = base_by_user.get(uid)
        if base:
            games_played = base.get("victory", 0) + base.get("draw", 0) + base.get("defeat", 0)
            return games_played >= game_week
    return False


_LAST_GAMEWEEK_CACHE: dict[tuple[str, int, int], tuple[float, int | None]] = {}
_LAST_GAMEWEEK_CACHE_TTL = 3600.0


def true_last_gameweek(short_id: str, season_number: int, division_number: int) -> int | None:
    """Port de mpg_app::_true_last_gameweek -- longueur REELLE de la phase
    en cours, lue via core.api.get_division_calendar (PAS deduite de
    poolGameweeks/Div_A_Gameweeks, qui representent la phase SUIVANTE, pas
    la courante -- bug B1 deja corrige cote mpg_app, a preserver). None si
    le calendrier n'est pas dispo (saison pas encore demarree) -- l'appelant
    garde alors son repli (cf. league_setup)."""
    cache_key = (short_id, season_number, division_number)
    now = time.monotonic()
    cached = _LAST_GAMEWEEK_CACHE.get(cache_key)
    if cached and now - cached[0] < _LAST_GAMEWEEK_CACHE_TTL:
        return cached[1]
    try:
        fixtures = get_division_calendar(short_id, season_number, division_number).get("fixtures") or []
        value = fixtures[-1]["gameWeek"] if fixtures else None
    except Exception:
        value = None
    _LAST_GAMEWEEK_CACHE[cache_key] = (now, value)
    return value


def division_delta(division_matches: list[dict], division_number: int, game_week: int,
                    league: dict, total_divisions: int) -> list[dict]:
    """Port de mpg_app::_division_delta -- standings d'UNE division pour la
    seule journee live en cours, meme format que aggregate_standings.
    Reutilise compute_matchday_standings tel quel, en injectant les champs
    absents du snapshot live (teamId/badges/bonuses -- informatifs,
    n'affectent aucun point)."""
    scoring_cfg = get_scoring_config(league)
    pool_gameweeks = league.get("poolGameweeks") or 0

    synth_matches = []
    for match in division_matches:
        synth = {"championshipGameWeekNumber": game_week}
        for side in ("home", "away"):
            team = dict(match[side])
            team.setdefault("teamId", None)
            team.setdefault("badges", [])
            team.setdefault("bonuses", [])
            synth[side] = team
        synth_matches.append(synth)

    matches_info = compute_matchday_standings(synth_matches, division_number, pool_gameweeks, total_divisions, scoring_cfg)
    return aggregate_standings(matches_info)


def combined_division_standings(base_by_user: dict, delta_rows: list[dict]) -> list[dict]:
    """Port de mpg_app::_combined_division_standings, reduit au schema
    d'archive v1 (pas de Precieux/Grotaldo -- absents de
    league_classement_archive.stats, cf. db/schema.sql)."""
    combined = []
    for row in delta_rows:
        uid = row["userId"]
        base = base_by_user.get(uid, {})
        base_victory = base.get("victory", 0.0)
        base_draw = base.get("draw", 0.0)
        base_defeat = base.get("defeat", 0.0)
        combined.append({
            "userId": uid,
            "teamName": row.get("teamName", ""),
            "points": _raw_points(base_victory, base_draw) + row["points"],
            "points_pond": base.get("points_pond", 0.0) + row["points_pond"],
            "buts_pour": base.get("score+", 0.0) + row["buts_pour"],
            "buts_contre": base.get("score-", 0.0) + row["buts_contre"],
            "victory": base_victory + row["victoires"],
            "draw": base_draw + row["nuls"],
            "defeat": base_defeat + row["defaites"],
            "matches_joues": base.get("matches_joues", 0) + row.get("matches_joues", 1),
            "cleanSheet": base.get("cleanSheet", 0) + row.get("cleanSheet", 0),
            "manita": base.get("manita", 0) + row.get("manita", 0),
            "on_fire": base.get("on_fire", 0) + row.get("on_fire", 0),
        })
    return combined


def league_setup(sb, league: dict, live_divisions: list[int], game_week: int) -> dict:
    """Parametres partages par toutes les divisions d'une ligue pour CE tick
    -- version reduite de mpg_app::_provisional_setup (base archivee lue par
    division plutot que d'un coup pour toute la ligue, cf.
    load_base_division_classement)."""
    base_by_division = {
        division: load_base_division_classement(sb, league["code"], league["seasonSearch"], division)
        for division in live_divisions
    }

    pool_gameweeks = league.get("poolGameweeks") or 0
    fallback_last_gameweek = pool_gameweeks if pool_gameweeks and game_week < pool_gameweeks else league.get("Div_A_Gameweeks")
    last_gameweek = fallback_last_gameweek
    sample_division = next(iter(live_divisions), None)
    if sample_division is not None and league.get("seasonSearch"):
        true_value = true_last_gameweek(league["code"], league["seasonSearch"], int(sample_division))
        if true_value is not None:
            last_gameweek = true_value

    return {
        "base_by_division": base_by_division,
        "internal_cfg": get_internal_bonus_config(league),
        "match_bonus_cfg": get_match_bonus_config(league),
        "season_number": league.get("seasonSearch"),
        "season_start": league.get("seasonStart"),
        "league_size": league.get("playersNumber"),
        "last_gameweek": last_gameweek,
    }


def resolve_division_rows(league: dict, division_number: int, division_matches: list[dict],
                           game_week: int, total_divisions: int, setup: dict) -> tuple[list[dict], bool]:
    """Classement resolu d'UNE division pour ce tick -- rang (3 premiers
    criteres MPG : points bruts, diff generale, attaque, meme sort que
    mpg_app::compute_provisional_division_classement -- le departage complet
    niveaux 4-6 de core.internal_bonus._rank_teams determine seulement QUI
    recoit Bonus_Champion/Second/Dernier ci-dessous, pas l'ordre affiche,
    comportement identique a mpg_app sur ce meme point). Renvoie (rows,
    is_live) -- is_live=False si cette journee est deja archivee (les
    bonus internes ne sont alors PAS recalcules : league_classement_archive
    ne les stocke pas en v1, cf. db/schema.sql -- pichichi/mur/boss
    retombent a 0/False jusqu'a la prochaine journee live, LIMITE CONNUE
    v1, a completer si l'archive stocke un jour ces valeurs)."""
    user_ids = [m[side].get("userId") for m in division_matches for side in ("home", "away")]
    base_by_user = setup["base_by_division"].get(division_number, {})

    if is_gameweek_archived(base_by_user, user_ids, game_week):
        rows = []
        for uid in user_ids:
            base = base_by_user.get(uid)
            if not base:
                continue
            rows.append({
                "userId": uid, "teamName": base.get("teamName", ""),
                "points": _raw_points(base.get("victory", 0), base.get("draw", 0)),
                "matches_joues": base.get("matches_joues", 0),
                "victoires": base.get("victory", 0), "nuls": base.get("draw", 0), "defaites": base.get("defeat", 0),
                "buts_pour": base.get("score+", 0), "buts_contre": base.get("score-", 0),
                "cleanSheet": base.get("cleanSheet", 0), "manita": base.get("manita", 0), "on_fire": base.get("on_fire", 0),
                "pichichi": 0, "mur": 0, "boss": False,
                "points_pond": base.get("points_pond", 0.0),
            })
        is_live = False
    else:
        delta_rows = division_delta(division_matches, division_number, game_week, league, total_divisions)
        combined = combined_division_standings(base_by_user, delta_rows)
        compute_internal_bonuses(
            combined, division_number, game_week, setup["last_gameweek"],
            setup["league_size"], setup["season_number"], setup["season_start"], league["nom"],
            setup["internal_cfg"], division_matches=division_matches,
        )
        rows = []
        for row in combined:
            bonus = row.pop("bonus_details", {})
            rows.append({
                "userId": row["userId"], "teamName": row.get("teamName", ""),
                "points": row["points"], "matches_joues": row["matches_joues"],
                "victoires": row["victory"], "nuls": row["draw"], "defaites": row["defeat"],
                "buts_pour": row["buts_pour"], "buts_contre": row["buts_contre"],
                "cleanSheet": row["cleanSheet"], "manita": row["manita"], "on_fire": row["on_fire"],
                "pichichi": bonus.get("Pichichi", 0), "mur": bonus.get("Le_Mur", 0),
                "boss": bool(bonus.get("Bonus_Champion", 0) > 0),
                "points_pond": row["points_pond"],
                # Internes -- consommes uniquement par resolve_league_wide_ranks,
                # jamais publies (cf. finalize_division_data).
                "_bonus_champion": bonus.get("Bonus_Champion", 0),
                "_bonus_podium": bonus.get("Bonus_Podium", 0),
            })
        is_live = True

    ranked = sorted(
        rows,
        key=lambda r: (-r["points"], -(r["buts_pour"] - r["buts_contre"]), -r["buts_pour"]),
    )
    for i, row in enumerate(ranked, start=1):
        row["rang"] = i
        row["diff"] = row["buts_pour"] - row["buts_contre"]
    return ranked, is_live


def resolve_league_wide_ranks(rows_by_division: dict[int, list[dict]], match_bonus_cfg: dict) -> dict[str, dict]:
    """Passe 2 : rang/points croises entre TOUTES les divisions d'une ligue,
    pour la carte "Ma situation" (rang_ligue/points_ligue).

    Formule SIMPLIFIEE v1 (decision utilisateur validee 2026-08-21) :
    points_pond + Pichichi + Le_Mur + Bonus_Champion + Bonus_Podium +
    compteurs de match (cleanSheet/manita/on_fire, gates par
    matchBonuses.enabled comme la formule officielle) -- SANS Precieux ni
    Grotaldo, aucune source Supabase pour l'un ou l'autre en v1 (cf. plan
    "Page Division pour mpg_live", Etape 3). Ne matchera donc PAS exactement
    le "points" officiel de mpg_app (core/live_projection.py::
    _finalize_league_row) tant que ces deux-la ne seront pas portes dans un
    chantier separe -- limite connue, documentee plutot que silencieuse."""
    mb_enabled = (match_bonus_cfg or {}).get("enabled", {})
    entries = []
    for division_rows in rows_by_division.values():
        for row in division_rows:
            live_counter_total = sum(row.get(k, 0) for k in LIVE_COUNTER_KEYS if mb_enabled.get(k, True))
            points_ligue = round(
                row.get("points_pond", 0.0)
                + row.get("pichichi", 0) + row.get("mur", 0)
                + row.get("_bonus_champion", 0) + row.get("_bonus_podium", 0)
                + live_counter_total,
                4,
            )
            entries.append((row["userId"], points_ligue))

    entries.sort(key=lambda e: -e[1])
    return {
        uid: {"rang_ligue": i, "points_ligue": points_ligue}
        for i, (uid, points_ligue) in enumerate(entries, start=1)
    }


def finalize_division_data(division_rows: list[dict], league_ranks: dict[str, dict],
                            team_names: dict[str, str] | None = None) -> list[dict]:
    """Assemble le tableau `data` final (cf. db/schema.sql,
    division_classement_live) : les champs publics de resolve_division_rows
    + rang_ligue/points_ligue (resolve_league_wide_ranks). Champs internes
    (_bonus_champion/_bonus_podium/points_pond) retires ici -- jamais
    publies au site.

    `team_names` : {userId: nom d'equipe MPG} (cf.
    core.api.get_division_team_names, appele par scripts/live_job.py) --
    aucun registre managers en v1 (decision utilisateur 2026-08-21), donc
    resolve_division_rows/combined_division_standings laissent toujours
    teamName vide (compute_matchday_standings ne le transporte pas) ;
    complete ici plutot que de refaire un appel HTTP par ligne."""
    team_names = team_names or {}
    out = []
    for row in division_rows:
        entry = {field: row.get(field) for field in PUBLIC_ROW_FIELDS}
        if team_names.get(row["userId"]):
            entry["teamName"] = team_names[row["userId"]]
        lr = league_ranks.get(row["userId"], {})
        entry["rang_ligue"] = lr.get("rang_ligue")
        entry["points_ligue"] = lr.get("points_ligue")
        out.append(entry)
    return out


def compute_super_classement(sb) -> list[dict]:
    """Classement croise TOUTES LIGUES CONFONDUES -- version volontairement
    reduite du Super Classement cote mpg_app (retour utilisateur 2026-08-23,
    "je te laisse poursuivre... tu connais la finalite" -- avance par etapes
    livrables plutot que de bloquer sur le port complet).

    Somme "points_ligue" (deja calcule par resolve_league_wide_ranks, cf.
    division_classement_live) pour chaque manager sur TOUTES les divisions
    de TOUTES les ligues ou il apparait -- couvre le cas d'un manager membre
    de plusieurs des ligues suivies. AUCUN des 15 bonus generaux
    (core/general_bonus.py cote mpg_app -- Sulfateuse/Winner/Gollum/etc.) ni
    Multi Boss n'est applique ici : leurs champs sources (owngoals, Ycards/
    Rcards, Precious_Count, les 8 compteurs *_DTC de La Piñata...) ne sont
    tout simplement pas suivis par le schema Supabase actuel
    (league_classement_archive.stats, cf. db/schema.sql) -- LIMITE CONNUE
    v1, a completer dans un chantier separe une fois ces champs ajoutes.
    Departage : points seuls (pas le tri a 6 criteres de mpg_app -- Ycards/
    Rcards absents ici pour la meme raison).

    PAS de filtre par "season" ici, volontairement -- deux ligues suivant la
    MEME saison calendaire reelle peuvent avoir des compteurs de saison MPG
    differents (ex. Ligue_2_EKT=21, Liga_Tapas=22 en meme temps, retour
    utilisateur 2026-08-23 : "une saison calendaire de foot peut regrouper
    plusieurs saisons MPG", meme piege deja documente cote mpg_app). Comme
    division_classement_live est upserte en place (PK sans game_week, cf.
    db/schema.sql), fusionner tout son contenu actuel donne naturellement
    l'etat "en ce moment", sans avoir a reconcilier des compteurs de saison
    incomparables entre ligues. La saison ECRITE dans super_classement (par
    l'appelant, cf. scripts/live_job.py) utilise plutot
    core.league.current_real_season_start_year() -- la meme regle
    calendaire que mpg_app, jamais un compteur de saison MPG d'une ligue en
    particulier.

    `teamName` : celui de la ligue ou ce manager a le plus de points_ligue
    (un manager peut avoir un nom d'equipe different par ligue, aucun nom
    "canonique" ici -- meilleur choix disponible sans registre managers)."""
    res = sb.table("division_classement_live").select("league_code,division,data").execute()
    rows = res.data or []

    totals: dict[str, float] = {}
    best_team_name: dict[str, tuple[float, str]] = {}
    for row in rows:
        for entry in (row.get("data") or []):
            uid = entry.get("userId")
            if not uid:
                continue
            points_ligue = entry.get("points_ligue") or 0
            totals[uid] = totals.get(uid, 0) + points_ligue
            current_best = best_team_name.get(uid, (-1, ""))
            if points_ligue > current_best[0] and entry.get("teamName"):
                best_team_name[uid] = (points_ligue, entry["teamName"])

    ranked = sorted(totals.items(), key=lambda kv: -kv[1])
    return [
        {"userId": uid, "teamName": best_team_name.get(uid, (0, ""))[1], "points": round(points, 1), "rang": i}
        for i, (uid, points) in enumerate(ranked, start=1)
    ]
