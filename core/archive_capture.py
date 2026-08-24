"""
Capture "en dur" d'une journee/division une fois MPG completement stabilise
(cf. core.live_scoring.all_division_matches_final) -- port verbatim de
mpg_app/core/archive_capture.py (aucune I/O fichier, aucun changement de
logique : mpg_live avait deja les deux dependances -- get_division_calendar
dans core/api.py, all_division_matches_final dans core/live_scoring.py --
portees precedemment pour true_last_gameweek).

Un appel unique par division/journee (get_division_matches +
get_division_calendar) suffit : une fois finalise, MPG a deja tout resolu
(score/badges/bonusesDetails, cf. docstring core.api.get_division_matches)
-- pas besoin de repasser par core.live_scoring.compute_division_live_scores
(reserve au live EN COURS, ou le score MPG n'est pas encore a jour). Sert de
"pass 1" au backfill (scripts/backfill_gameweek.py) : le JSON capture ici
est ensuite reutilise tel quel par core.scoring/core.internal_bonus, sans
aucun autre appel API MPG (retour utilisateur 2026-08-23, "en 2 passes ...
eviter de multiples appels API MPG").
"""
from datetime import datetime, timezone

from core.api import get_division_matches, get_division_calendar
from core.live_scoring import all_division_matches_final


def _teamid_to_userid(division_matches: list[dict]) -> dict[str, str]:
    """{teamId: userId} pour toutes les equipes vues dans ces matchs --
    necessaire pour traduire previousTargetMan/afterTargetMan (calendrier,
    des teamId, cf. get_division_calendar) en userId."""
    mapping: dict[str, str] = {}
    for match in division_matches:
        for side in ("home", "away"):
            team = match.get(side) or {}
            team_id, user_id = team.get("teamId"), team.get("userId")
            if team_id and user_id:
                mapping[team_id] = user_id
    return mapping


def resolve_precious_holder_user_id(calendar: dict, game_week: int, teamid_to_userid: dict[str, str]) -> str | None:
    """userId du detenteur du Precieux APRES cette journee. None si non
    resolu/absent -- capture, pas encore consomme en aval cote mpg_live
    (aucune colonne Precieux dans league_classement_archive en v1)."""
    for fixture in calendar.get("fixtures", []):
        if fixture.get("gameWeek") == game_week:
            team_id = fixture.get("afterTargetMan")
            return teamid_to_userid.get(team_id) if team_id else None
    return None


# {cle brute de match[cote]["bonuses"] : cle DTC correspondante} -- retour
# utilisateur 2026-08-24 ("La Piñata est un cumul du nombre de coups/bonus
# subis... en theorie c'est documente", confirme via Corrections_pour_
# Sep.md/PINATA_DTC_WEIGHTS cote mpg_app). "fourStrikers" (424/QuatDecat)
# absent de core.live_scoring.VALID_BONUSES (n'affecte pas la note, cf.
# Reponse_audit_pour_Ilan.md "424 -- rien a coder") mais reste un coup
# DECLARE au meme titre que les 7 autres pour la Piñata. "removeRandomPlayer"
# (Chapron Rouge, abandonne 2025-2026) volontairement absent : ne fait pas
# partie des 8 cles PINATA_DTC_WEIGHTS cote mpg_app.
RAW_BONUS_TO_DTC_KEY = {
    "boostAllPlayers": "zahia_DTC",        # Zahia
    "boostOnePlayer": "mcdo_DTC",          # McDo+
    "nerfGoalkeeper": "suarez_DTC",        # Suarez
    "nerfAllPlayers": "cheatCode_DTC",     # Cheat Code
    "blockTacticalSubs": "tontonPat_DTC",  # Tonton Pat'
    "removeGoal": "nanard_DTC",            # Valise a Nanard
    "mirror": "mirror_DTC",                # Miroir
    "fourStrikers": "QuatDecat_DTC",       # 424
}


def dtc_counts_from_matches(division_matches: list[dict]) -> dict[str, dict[str, int]]:
    """{userId: {dtc_key: count}} pour TOUS les matchs d'une journee/division
    -- les coups DECLARES par une equipe (match[cote]["bonuses"], cle
    presente quelle que soit sa valeur) sont attribues comme SUBIS a
    l'equipe ADVERSE. Meme principe que mpg_app/backfill_historical_
    season.py::bonuses_subis_from_match (presence 0/1 par match : un meme
    coup ne peut jamais compter deux fois dans un seul match), applique ici
    directement sur TOUTE une journee d'un coup plutot que match par match
    (un manager ne joue qu'un seul match par journee dans sa division, donc
    aucun risque de double-compte)."""
    result: dict[str, dict[str, int]] = {}
    for match in division_matches:
        home = match.get("home") or {}
        away = match.get("away") or {}
        home_user, away_user = home.get("userId"), away.get("userId")
        home_cast = set(home.get("bonuses") or {}) & set(RAW_BONUS_TO_DTC_KEY)
        away_cast = set(away.get("bonuses") or {}) & set(RAW_BONUS_TO_DTC_KEY)
        if away_user and home_cast:
            acc = result.setdefault(away_user, {})
            for raw_key in home_cast:
                dtc_key = RAW_BONUS_TO_DTC_KEY[raw_key]
                acc[dtc_key] = acc.get(dtc_key, 0) + 1
        if home_user and away_cast:
            acc = result.setdefault(home_user, {})
            for raw_key in away_cast:
                dtc_key = RAW_BONUS_TO_DTC_KEY[raw_key]
                acc[dtc_key] = acc.get(dtc_key, 0) + 1
    return result


def capture_division_journee(
    short_id: str, season_number: int, division_number: int, game_week: int, token: str = None,
) -> dict | None:
    """Capture "en dur" d'UNE journee/division -- None si MPG n'a pas encore
    tout stabilise (cf. all_division_matches_final), auquel cas reessayer
    plus tard. Une fois pret, renvoie :
    {"shortId", "seasonNumber", "divisionNumber", "gameWeek",
     "capturedAt" (ISO UTC), "divisionMatches" (tel que get_division_matches,
     MPG a deja tout resolu), "preciousHolderUserId"}."""
    division_matches = get_division_matches(short_id, season_number, division_number, game_week, token)
    if not all_division_matches_final(division_matches):
        return None

    calendar = get_division_calendar(short_id, season_number, division_number, token)
    teamid_to_userid = _teamid_to_userid(division_matches)
    precious_holder = resolve_precious_holder_user_id(calendar, game_week, teamid_to_userid)

    return {
        "shortId": short_id, "seasonNumber": season_number, "divisionNumber": division_number,
        "gameWeek": game_week, "capturedAt": datetime.now(timezone.utc).isoformat(),
        "divisionMatches": division_matches, "preciousHolderUserId": precious_holder,
    }
