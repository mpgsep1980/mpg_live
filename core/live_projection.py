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
from core.archive_capture import RAW_BONUS_TO_DTC_KEY
from core.league import get_scoring_config, get_internal_bonus_config, get_match_bonus_config
from core.scoring import compute_matchday_standings, aggregate_standings
from core.internal_bonus import compute_internal_bonuses

DTC_KEYS = tuple(RAW_BONUS_TO_DTC_KEY.values())

LIVE_COUNTER_KEYS = ("cleanSheet", "manita", "on_fire")

# Champs publics ecrits dans division_classement_live.data (cf. db/schema.sql)
# -- tout le reste (points_pond, _bonus_champion, _bonus_podium) est interne,
# utilise seulement par resolve_league_wide_ranks, jamais publie tel quel.
# grotaldo/owngoals : PAS affiches sur site/division.html (colonnes non
# demandees pour cette page), mais necessaires ici pour alimenter les bonus
# generaux du Super Classement (King Grotaldo/Harry Maguire Challenge, cf.
# core/general_bonus.py::compute_super_classement) -- les donnees stockees
# peuvent etre plus riches que ce qu'une page en particulier affiche.
PUBLIC_ROW_FIELDS = (
    "userId", "teamName", "rang", "points", "matches_joues",
    "victoires", "nuls", "defaites", "buts_pour", "buts_contre", "diff",
    "cleanSheet", "manita", "on_fire", "grotaldo", "owngoals", "pichichi", "mur", "boss",
    # boss_saison/bonus_second/bonus_dernier : PAS internes comme
    # _bonus_champion/_precious -- lus directement par compute_super_classement
    # (Multi Boss/La Triplette/Poulidor/La Chèvre operent au niveau Super
    # Classement -- bonus generaux/inter-ligues -- pas resolve_league_wide_ranks).
    "boss_saison", "bonus_second", "bonus_dernier", "precious_count",
    *DTC_KEYS,
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
    d'archive v1 (pas de Precieux -- aucune source Supabase, cf.
    db/schema.sql). grotaldo/owngoals AJOUTES (retour utilisateur
    2026-08-23, "on y va etape par etape" -- deja calcules par
    aggregate_standings, simplement pas encore reportes ici) pour
    debloquer King Grotaldo/Harry Maguire Challenge dans les bonus
    generaux (cf. core/general_bonus.py, core/live_projection.py::
    compute_super_classement)."""
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
            "grotaldo": base.get("grotaldo", 0) + row.get("grotaldo", 0),
            "owngoals": base.get("owngoals", 0) + row.get("owngoals", 0),
            # Ycards/Rcards AJOUTES (retour utilisateur 2026-08-24) -- deja
            # calcules par aggregate_standings, necessaires pour departager
            # resolve_league_wide_ranks (meme derniers criteres que le
            # tri a 6 niveaux de Super_Classement_General_V2.ipynb : des
            # egalites exactes points/diff/attaque sont frequentes en debut
            # de saison, trouve via un Podium/Super_Podium visiblement faux
            # sur 4 managers a egalite parfaite).
            "Ycards": base.get("Ycards", 0) + row.get("Ycards", 0),
            "Rcards": base.get("Rcards", 0) + row.get("Rcards", 0),
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


def _rows_from_archive(base_by_user: dict) -> list[dict]:
    """Lignes de classement construites UNIQUEMENT depuis l'archive (aucun
    division_matches necessaire) -- factorise depuis l'ancienne branche
    "deja archivee" de resolve_division_rows pour etre aussi reutilise par
    refresh_division_classement_from_archive (retour utilisateur 2026-08-24 :
    une division reste vide sur site/division.html tant qu'aucune fenetre
    live n'a jamais ete pollee, meme si son archive existe deja).
    pichichi/mur/bonus_champion/bonus_podium lus directement depuis
    l'archive (retour utilisateur 2026-08-24, "recupere la logique de
    points qu'on a definie dans le document partage avec Ilan" --
    core/archive.py appelle desormais compute_internal_bonuses a
    l'archivage, comme le fait deja le chemin live ci-dessous -- ces champs
    ne retombent plus a 0/False. Repli a 0/False conserve seulement pour
    une archive ANTERIEURE a ce changement (pas encore regeneree)."""
    rows = []
    for uid, base in base_by_user.items():
        rows.append({
            "userId": uid, "teamName": base.get("teamName", ""),
            "points": _raw_points(base.get("victory", 0), base.get("draw", 0)),
            "matches_joues": base.get("matches_joues", 0),
            "victoires": base.get("victory", 0), "nuls": base.get("draw", 0), "defaites": base.get("defeat", 0),
            "buts_pour": base.get("score+", 0), "buts_contre": base.get("score-", 0),
            "cleanSheet": base.get("cleanSheet", 0), "manita": base.get("manita", 0), "on_fire": base.get("on_fire", 0),
            "grotaldo": base.get("grotaldo", 0), "owngoals": base.get("owngoals", 0),
            "Ycards": base.get("Ycards", 0), "Rcards": base.get("Rcards", 0),
            "pichichi": base.get("pichichi", 0), "mur": base.get("mur", 0),
            "boss": bool(base.get("bonus_champion", 0) > 0),
            "boss_saison": base.get("boss_saison", 0),
            "bonus_second": base.get("bonus_second", 0), "bonus_dernier": base.get("bonus_dernier", 0),
            "precious_count": base.get("precious_count", 0),
            **{key: base.get(key, 0) for key in DTC_KEYS},
            "points_pond": base.get("points_pond", 0.0),
            "_bonus_champion": base.get("bonus_champion", 0), "_bonus_podium": base.get("bonus_podium", 0),
            "_precious": base.get("precious", 0),
        })
    return rows


def _rank_rows(rows: list[dict]) -> list[dict]:
    """Rang + diff (3 premiers criteres MPG : points bruts, diff generale,
    attaque) -- factorise depuis la fin de resolve_division_rows, partage
    par les deux branches (live/archivee) ET par
    refresh_division_classement_from_archive."""
    ranked = sorted(
        rows,
        key=lambda r: (-r["points"], -(r["buts_pour"] - r["buts_contre"]), -r["buts_pour"]),
    )
    for i, row in enumerate(ranked, start=1):
        row["rang"] = i
        row["diff"] = row["buts_pour"] - row["buts_contre"]
    return ranked


def refresh_division_classement_from_archive(sb, league: dict, division: int) -> tuple[list[dict], int]:
    """Reconstruit les lignes de classement d'UNE division UNIQUEMENT depuis
    league_classement_archive, sans poll live -- pour que site/division.html
    affiche deja quelque chose entre deux fenetres, meme pour une division
    jamais encore pollee en direct (retour utilisateur 2026-08-24, apres
    avoir constate une page vide malgre une journee deja archivee a la
    main). Renvoie (rows, last_game_week) -- ([], 0) si rien d'archive.
    last_game_week est approxime par le max de matches_joues (meme
    convention que is_gameweek_archived ailleurs dans ce module) --
    l'archive ne stocke pas le numero de journee directement."""
    base_by_user = load_base_division_classement(sb, league["code"], league["seasonSearch"], division)
    if not base_by_user:
        return [], 0
    last_game_week = max((b.get("matches_joues", 0) for b in base_by_user.values()), default=0)
    return _rank_rows(_rows_from_archive(base_by_user)), last_game_week


def resolve_division_rows(league: dict, division_number: int, division_matches: list[dict],
                           game_week: int, total_divisions: int, setup: dict) -> tuple[list[dict], bool]:
    """Classement resolu d'UNE division pour ce tick -- rang (3 premiers
    criteres MPG : points bruts, diff generale, attaque, meme sort que
    mpg_app::compute_provisional_division_classement -- le departage complet
    niveaux 4-6 de core.internal_bonus._rank_teams determine seulement QUI
    recoit Bonus_Champion/Second/Dernier ci-dessous, pas l'ordre affiche,
    comportement identique a mpg_app sur ce meme point). Renvoie (rows,
    is_live) -- is_live=False si cette journee est deja archivee (les
    bonus internes ne sont alors PAS recalcules en direct ici, mais lus
    depuis league_classement_archive, qui les stocke desormais -- cf.
    core/archive.py::archive_closed_gameweek_if_needed, retour utilisateur
    2026-08-24 -- pichichi/mur/boss refletent la derniere archive, pas
    "0/False" comme avant ce correctif)."""
    user_ids = [m[side].get("userId") for m in division_matches for side in ("home", "away")]
    base_by_user = setup["base_by_division"].get(division_number, {})

    if is_gameweek_archived(base_by_user, user_ids, game_week):
        rows = _rows_from_archive({uid: base_by_user[uid] for uid in user_ids if uid in base_by_user})
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
                "grotaldo": row["grotaldo"], "owngoals": row["owngoals"],
                "Ycards": row.get("Ycards", 0), "Rcards": row.get("Rcards", 0),
                "pichichi": bonus.get("Pichichi", 0), "mur": bonus.get("Le_Mur", 0),
                "boss": bool(bonus.get("Bonus_Champion", 0) > 0),
                "boss_saison": next((v for k, v in bonus.items() if "_Boss_Saison_" in k), 0),
                "bonus_second": bonus.get("Bonus_Second", 0), "bonus_dernier": bonus.get("Bonus_Dernier", 0),
                # precious_count : jamais incremente EN COURS de journee (le
                # detenteur du Precieux n'est resoluble qu'a l'archivage, cf.
                # _precious=0 plus bas) -- reporte tel quel depuis la
                # derniere archive, incremente seulement dans core/archive.py.
                "precious_count": base_by_user.get(row["userId"], {}).get("precious_count", 0),
                # *_DTC (La Piñata) : meme raison que precious_count --
                # jamais recompte EN COURS de journee (core/archive_capture.py
                # ::dtc_counts_from_matches n'est appele qu'a l'archivage),
                # reporte tel quel.
                **{key: base_by_user.get(row["userId"], {}).get(key, 0) for key in DTC_KEYS},
                "points_pond": row["points_pond"],
                # Internes -- consommes uniquement par resolve_league_wide_ranks,
                # jamais publies (cf. finalize_division_data). _precious=0
                # ici : Mon Precieux n'est resoluble QUE via get_division_calendar
                # (core/archive_capture.py), jamais dispo pendant une journee
                # encore en cours -- meme limite que cote mpg_app (cf.
                # docstring get_division_calendar, "absents pour les journees
                # dont le vrai resultat n'est pas encore confirme").
                "_bonus_champion": bonus.get("Bonus_Champion", 0),
                "_bonus_podium": bonus.get("Bonus_Podium", 0),
                "_precious": 0,
            })
        is_live = True

    return _rank_rows(rows), is_live


def resolve_league_wide_ranks(rows_by_division: dict[int, list[dict]], match_bonus_cfg: dict) -> dict[str, dict]:
    """Passe 2 : rang/points croises entre TOUTES les divisions d'une ligue,
    pour la carte "Ma situation" (rang_ligue/points_ligue).

    Formule OFFICIELLE (retour utilisateur 2026-08-24, "recupere la logique
    de points qu'on a definie dans le document partage avec Ilan, on n'a
    pas besoin de reinventer la roue" -- retrouvee dans le notebook de
    production mpg_app, calculate_total_points,
    MPG_Ligue_2_EKT_Test_2025.ipynb, verifiee terme a terme contre un
    classement reel Ligue_Camembert) :
    points_pond + Pichichi + Le_Mur + Bonus_Champion + Bonus_Podium +
    cleanSheet + manita + on_fire + Precious - Grotaldo. Les 5 derniers
    termes sont gates par matchBonuses.enabled (meme mecanisme que la
    formule officielle -- decision LDC "bonus de match neutralises", cf.
    Reponse_audit_pour_Ilan.md section 6). _precious vaut toujours 0 sur
    une division encore EN COURS (resolu uniquement a l'archivage, cf.
    core/archive_capture.py) -- points_ligue peut donc sous-compter jusqu'a
    1 point pour le detenteur pendant qu'une journee est encore live,
    corrige des qu'elle est archivee."""
    mb_enabled = (match_bonus_cfg or {}).get("enabled", {})
    entries = []
    for division_rows in rows_by_division.values():
        for row in division_rows:
            live_counter_total = sum(row.get(k, 0) for k in LIVE_COUNTER_KEYS if mb_enabled.get(k, True))
            grotaldo_term = row.get("grotaldo", 0) if mb_enabled.get("grotaldo", True) else 0
            precious_term = row.get("_precious", 0) if mb_enabled.get("precious", True) else 0
            points_ligue = round(
                row.get("points_pond", 0.0)
                + row.get("pichichi", 0) + row.get("mur", 0)
                + row.get("_bonus_champion", 0) + row.get("_bonus_podium", 0)
                + live_counter_total
                + precious_term - grotaldo_term,
                4,
            )
            diff = (row.get("buts_pour") or 0) - (row.get("buts_contre") or 0)
            entries.append((
                row["userId"], points_ligue, diff, row.get("buts_pour") or 0,
                row.get("Ycards", 0), row.get("Rcards", 0),
            ))

    # Depart les egalites (frequent en debut de saison -- retour utilisateur
    # 2026-08-24, trouve via un Podium/Super_Podium visiblement faux : 4
    # managers a egalite exacte de points_ligue/diff/attaque partaient dans
    # un ordre arbitraire, dependant de l'ordre d'iteration des divisions,
    # faute de tiebreak ici). Meme 6 criteres que le tri du Super Classement
    # officiel (Super_Classement_General_V2.ipynb cellule 3 : points, diff,
    # buts_pour, buts_contre, Ycards, Rcards -- moins de cartons gagne).
    entries.sort(key=lambda e: (-e[1], -e[2], -e[3], e[4], e[5]))
    return {
        uid: {"rang_ligue": i, "points_ligue": points_ligue}
        for i, (uid, points_ligue, *_rest) in enumerate(entries, start=1)
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
    "on y va etape par etape" -- avance par etapes livrables plutot que de
    bloquer sur le port complet).

    Somme "points_ligue" (deja calcule par resolve_league_wide_ranks, cf.
    division_classement_live) POUR LA BASE, plus les champs bruts necessaires
    aux bonus generaux (score+/score-/victory/draw/defeat/cleanSheet/manita/
    on_fire/grotaldo/owngoals) pour chaque manager sur TOUTES les divisions
    de TOUTES les ligues ou il apparait -- couvre le cas d'un manager membre
    de plusieurs des ligues suivies. apply_general_bonuses (core/general_bonus.py,
    port verbatim de mpg_app) est ensuite applique sur ce total -- SEULES les
    categories dont la cle source est fournie ci-dessus s'activent
    reellement (Sulfateuse/Rideau de Fer/Winner/En feu/Pétard Mouillé/
    Passoire/L'Araignée/High Five/FFL/Macroniste/King Grotaldo/Harry Maguire
    Challenge -- 12 sur 16), les 4 autres (La Piñata/Gollum/Poulidor/La
    Chèvre) restent a 0 pour tout le monde -- leurs champs sources ne sont
    pas suivis par le schema Supabase actuel, LIMITE CONNUE v1, a completer
    separement.

    Multi Boss / La Triplette / Podium / Super_Podium AJOUTES (retour
    utilisateur 2026-08-24, "vas-y" -- port de Super_Classement_General_V2.
    ipynb cellule 6, LDC exclue partout comme dans la source) : Multi Boss
    (somme des Boss_Saison si champion d'au moins 3 ligues), La Triplette
    (+10 par tranche de 3 titres Boss_Saison dans UNE MEME ligue -- inerte
    tant qu'une seule saison est archivee par ligue, aucune donnee
    multi-saison pour l'instant, redemarrera seul une fois l'historique
    accumule), Podium (+20/+10/+5 pour rang_ligue 1/2/3), Super_Podium
    (+10 pour qui a le plus de podiums). La Chèvre (place cote notebook
    seuille en dur a 48) volontairement PAS portee : 0 point, purement
    informative, et le seuil ne serait pas fiable sur nos ligues a 72
    joueurs.

    Departage : points seuls a CE niveau (Super Classement) -- Ycards/Rcards
    sont bien suivis desormais (retour utilisateur 2026-08-24, meme correctif
    que ci-dessus), mais uniquement consommes par resolve_league_wide_ranks
    pour departager rang_ligue/Podium ; pas de second niveau de tri ici,
    contrairement au tri a 6 criteres du notebook a ce meme etage.

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
    from core.general_bonus import apply_general_bonuses

    leagues_res = sb.table("leagues").select("nom,code").execute()
    ldc_code = next((r["code"] for r in (leagues_res.data or []) if r["nom"] == "Ligue_des_Champignons"), None)

    res = sb.table("division_classement_live").select("league_code,division,data").execute()
    rows = res.data or []

    SUM_FIELDS = (
        "score+", "score-", "victory", "draw", "defeat",
        "cleanSheet", "manita", "on_fire", "grotaldo", "owngoals",
        "Bonus_Second", "Bonus_Dernier", "Precious_Count",
        *DTC_KEYS,
    )
    ENTRY_FIELD_MAP = {
        "score+": "buts_pour", "score-": "buts_contre", "victory": "victoires",
        "draw": "nuls", "defeat": "defaites",
        "cleanSheet": "cleanSheet", "manita": "manita", "on_fire": "on_fire",
        "grotaldo": "grotaldo", "owngoals": "owngoals",
        # Bonus_Second/Bonus_Dernier -- active Poulidor/La Chèvre dans
        # apply_general_bonuses (retour utilisateur 2026-08-24, deja
        # calcules par compute_internal_bonuses, simplement jamais
        # surfaces jusqu'ici -- meme cas que Boss_Saison plus haut).
        "Bonus_Second": "bonus_second", "Bonus_Dernier": "bonus_dernier",
        # Precious_Count -- active Gollum (cumul saison du detenteur du
        # Precieux, PAS juste le detenteur actuel -- cf. Corrections_pour_
        # Sep.md section B3, retour utilisateur 2026-08-24 "documente dans
        # le Git avec Ilan").
        "Precious_Count": "precious_count",
        # *_DTC (8 compteurs) -- active La Piñata (retour utilisateur
        # 2026-08-24, meme reconstruction que mpg_app/backfill_historical_
        # season.py::bonuses_subis_from_match, cf. core/archive_capture.py::
        # dtc_counts_from_matches) -- PinataScore precalcule juste avant
        # apply_general_bonuses ci-dessous, meme cle par cle que le reste.
        **{key: key for key in DTC_KEYS},
    }

    stats_by_user: dict[str, dict] = {}
    best_team_name: dict[str, tuple[float, str]] = {}
    # {userId: {league_code: valeur}} -- max/min par ligue (une ligue peut
    # avoir plusieurs divisions, donc plusieurs entrees pour le meme
    # manager n'arrivent normalement pas, mais max()/min() protegent quand
    # meme contre un double-compte si jamais).
    boss_saison_by_user_league: dict[str, dict[str, float]] = {}
    place_by_user_league: dict[str, dict[str, int]] = {}

    for row in rows:
        league_code = row["league_code"]
        for entry in (row.get("data") or []):
            uid = entry.get("userId")
            if not uid:
                continue
            stats = stats_by_user.setdefault(uid, {"points": 0.0, **{k: 0 for k in SUM_FIELDS}})
            points_ligue = entry.get("points_ligue") or 0
            stats["points"] += points_ligue
            for target_key, entry_key in ENTRY_FIELD_MAP.items():
                stats[target_key] += entry.get(entry_key) or 0

            current_best = best_team_name.get(uid, (-1, ""))
            if points_ligue > current_best[0] and entry.get("teamName"):
                best_team_name[uid] = (points_ligue, entry["teamName"])

            if league_code != ldc_code:
                boss_saison = entry.get("boss_saison") or 0
                if boss_saison:
                    d = boss_saison_by_user_league.setdefault(uid, {})
                    d[league_code] = max(d.get(league_code, 0), boss_saison)
                rang_ligue = entry.get("rang_ligue")
                if rang_ligue is not None:
                    d = place_by_user_league.setdefault(uid, {})
                    d[league_code] = min(d.get(league_code, rang_ligue), rang_ligue)

    from core.general_bonus import compute_pinata_score
    for stats in stats_by_user.values():
        stats["PinataScore"] = compute_pinata_score(stats)

    # Instantane des valeurs BRUTES (avant qu'apply_general_bonuses ne les
    # transforme en points/bonus_details) -- retour utilisateur 2026-08-24,
    # "Trophees se trouve dans Super_Classement_General_V2.ipynb" : cet
    # onglet affiche le classement COMPLET par categorie (pas juste le
    # gagnant), il faut donc garder la valeur brute de chacun, pas
    # seulement bonus_details qui ne retient que 0/points du gagnant.
    RAW_STAT_KEYS = (*SUM_FIELDS, "PinataScore")
    for stats in stats_by_user.values():
        stats["raw_stats"] = {key: stats.get(key, 0) for key in RAW_STAT_KEYS}

    classement = list(stats_by_user.items())  # [(userId, stats), ...] -- format attendu par apply_general_bonuses
    apply_general_bonuses(classement)

    # Multi Boss / La Triplette -- port de Super_Classement_General_V2.ipynb
    # cellule 6 ("Verification du Multi Boss"/"Triple Boss"). Un seul
    # Boss_Saison par (user, ligue) dans nos donnees actuelles (pas
    # d'historique multi-saison) : La Triplette (>=3 titres dans UNE MEME
    # ligue) ne peut donc jamais se declencher pour l'instant -- ecrit
    # quand meme fidelement, redemarrera de lui-meme une fois plusieurs
    # saisons archivees par ligue.
    for uid, stats in classement:
        boss_by_league = boss_saison_by_user_league.get(uid, {})
        bonus_details = stats["bonus_details"]
        if len(boss_by_league) >= 3:
            multi_boss_points = sum(sorted(boss_by_league.values(), reverse=True))
            stats["points"] += multi_boss_points
            bonus_details["Multi Boss"] = multi_boss_points

        # Chaque ligue ne contribue qu'UN SEUL titre visible actuellement
        # (cf. commentaire ci-dessus) -- la ligne ci-dessous reste ecrite
        # comme le notebook (comptage par cle Boss_Saison) pour rester
        # correcte le jour ou plusieurs saisons/ligue seront disponibles.
        ligue_boss_counts: dict[str, int] = {lc: 1 for lc in boss_by_league}
        triple_boss_points = sum((count // 3) * 10 for count in ligue_boss_counts.values())
        if triple_boss_points:
            stats["points"] += triple_boss_points
            bonus_details["La Triplette"] = triple_boss_points

    # Podium -- +20/+10/+5 pour rang_ligue 1/2/3 (au mieux si un manager
    # apparait dans plusieurs divisions de la meme ligue, cas normalement
    # impossible mais protege comme le notebook, qui prend le meilleur
    # rang connu par ligue).
    podium_points_map = {1: 20, 2: 10, 3: 5}
    podium_counts: dict[str, int] = {}
    for uid, stats in classement:
        bonus_details = stats["bonus_details"]
        for league_code, place in place_by_user_league.get(uid, {}).items():
            points = podium_points_map.get(place)
            if points is None:
                continue
            bonus_details[f"{league_code}_Podium_{place:02d}"] = points
            stats["points"] += points
            podium_counts[uid] = podium_counts.get(uid, 0) + 1

    # Super_Podium -- +10 pour qui cumule le plus de podiums (toutes ligues
    # confondues), tous ex-aequo recompenses comme le notebook.
    max_podiums = max(podium_counts.values(), default=0)
    if max_podiums > 0:
        for uid, stats in classement:
            if podium_counts.get(uid, 0) == max_podiums:
                stats["points"] += 10
                stats["bonus_details"]["Super_Podium"] = 10

    ranked = sorted(classement, key=lambda kv: -kv[1]["points"])
    return [
        {
            "userId": uid, "teamName": best_team_name.get(uid, (0, ""))[1],
            "points": round(stats["points"], 1), "bonus_details": stats["bonus_details"], "rang": i,
            "raw_stats": stats["raw_stats"],
        }
        for i, (uid, stats) in enumerate(ranked, start=1)
    ]
