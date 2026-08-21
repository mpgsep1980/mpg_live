"""
Moteur de calcul du classement d'une journée MPG, à partir des données brutes
de l'API (`/division/mpg_division_{shortId}_{season}_{division}/game-week/{gw}/matches`,
champ `divisionMatches`).

Port de mpg_app/core/scoring.py -- identique sauf `enrich_with_managers`,
retiree ici (depend de core.users/MPG_Users.json, sans equivalent Supabase
en v1 ; RealName/teamName restent le nom d'equipe MPG, deja porte par les
donnees brutes -- voir core/live_projection.py::resolve_division_rows).
"""

# Labels FR des badges/bonus MPG suivis (informatifs — n'affectent aucun point).
# Clé = champ brut renvoyé par l'API MPG dans match[side]["badges"] / ["bonuses"].
BONUS_LABELS = {
    "badCoaching": "Mauvais coaching",
    "goodCoaching": "Bon coaching",
    "badSuitcase": "Valise en plomb",
    "goldenSuitcase": "Valise en or",
    "punishment": "La punition",
    "onFire": "5 buts réels",
    "spanking": "La fessée",
    "goldenDoc": "McDo en or",
    "goldenVicky": "Zahia en or",
    "goldenSuarez": "Suarez en or",
    "noDuplicatedClubs": "Gifoutou",
    "lateLoser": "Seum",
    "bigRotaldo": "Grotaldo",
    "blockTacticalSubs": "Tonton Pat'",
    "removeRandomPlayer": "Chapron Rouge",
    "mirror": "Miroir",
    "fourStrikers": "424",
    "boostAllPlayers": "Zahia",
    "boostOnePlayer": "McDo+",
    "nerfGoalkeeper": "Suarez",
    "nerfAllPlayers": "Cheat Code 18-26",
    "removeGoal": "Valise à Nanard",
}

# Valeurs par défaut — règle MPG en vigueur depuis la saison 2025-2026 : les points
# pondérés sont multipliés (plus divisés) par un coefficient qui dépend du tiers
# (haut/milieu/bas) où se trouve la division, PAS d'un numéro de division fixe —
# les tiers se répartissent proportionnellement au nombre total de divisions de la
# ligue (ex. 6 divisions -> tiers de 2 : {1,2}=1.0 {3,4}=0.9 {5,6}=0.8 ; 9 divisions
# -> tiers de 3 : {1,2,3}=1.0 {4,5,6}=0.9 {7,8,9}=0.8). Une ligue peut surcharger
# tout ou partie de ces clés via leagues.scoring (Supabase, editable depuis l'admin).
DEFAULT_SCORING_CONFIG = {
    "playersPerDivision": 8,
    "poolCoeff": 0.9,
    "coeffTiers": [1.0, 0.9, 0.8],
    "defaultCoeff": 1.0,
    "trackedBonuses": list(BONUS_LABELS.keys()),
}


def _coeff(
    championship_game_week_number: int,
    division_number: int,
    pool_gameweeks: int,
    total_divisions: int,
    scoring_config: dict,
) -> float:
    """Coefficient multiplicateur des points, selon la journée et la division
    (V=3 -> 3*coeff, N=1 -> 1*coeff, D=0 -> 0)."""
    if championship_game_week_number < pool_gameweeks:
        return scoring_config["poolCoeff"]
    tiers = scoring_config["coeffTiers"]
    if not total_divisions or division_number < 1:
        return scoring_config["defaultCoeff"]
    tier_size = max(1, total_divisions // len(tiers))
    tier_index = min(len(tiers) - 1, (division_number - 1) // tier_size)
    return tiers[tier_index]


def _extract_badges(team: dict, tracked_keys: list[str]) -> dict:
    present = set(team.get("badges") or []) | set(team.get("bonuses") or [])
    return {key: (key in present) for key in tracked_keys}


def _rotaldo_count(team: dict) -> int:
    """Nombre de titulaires Rotaldo (absents, aucun remplacant valide trouve)
    dans CE match -- MPG place un joueur PLACEHOLDER litteral
    lastName=="Rotaldo" par slot non pourvu."""
    return sum(1 for p in (team.get("players") or {}).values() if p.get("lastName") == "Rotaldo")


def _player_sums(team: dict) -> dict:
    """Cumule les stats joueur d'une equipe pour UN match -- ownGoals (hors
    Rotaldo), cartons, buts MPG (mpgGoals, absent des saisons les plus
    anciennes -- .get defensif)."""
    players = (team.get("players") or {}).values()
    owngoals = sum(
        p.get("ownGoals", 0) for p in players
        if isinstance(p.get("ownGoals"), (int, float)) and p.get("lastName") != "Rotaldo"
    )
    ycards = sum(1 for p in players if p.get("card") == 1)
    rcards = sum(1 for p in players if p.get("card") == 2)
    mpggoals = sum(p.get("mpgGoals", 0) for p in players if isinstance(p.get("mpgGoals"), (int, float)))
    return {"owngoals": owngoals, "ycards": ycards, "rcards": rcards, "mpggoals": mpggoals}


def _team_result(own_score: int, opp_score: int) -> tuple[int, str]:
    if own_score > opp_score:
        return 3, "V"
    if own_score == opp_score:
        return 1, "N"
    return 0, "D"


def compute_matchday_standings(
    division_matches: list[dict],
    division_number: int,
    pool_gameweeks: int,
    total_divisions: int,
    scoring_config: dict | None = None,
) -> list[dict]:
    """Calcule points bruts et pondérés de chaque manager pour une liste de matchs
    (une journée/division). Fonctionne aussi bien sur des matchs terminés que sur
    des matchs en cours (le score MPG en cours de match est déjà dans `score`).

    `total_divisions` : nombre total de divisions de la phase finale de la ligue
    pour cette saison -- sert à répartir les tiers de `coeffTiers` proportionnellement
    (voir _coeff).
    `scoring_config` : voir DEFAULT_SCORING_CONFIG — passer `get_scoring_config(league)`
    (core/league.py) pour appliquer les surcharges éventuelles de `leagues.scoring`."""
    cfg = scoring_config or DEFAULT_SCORING_CONFIG
    matches_info = []
    for match in division_matches:
        gw = match.get("championshipGameWeekNumber", 0)
        coeff = _coeff(gw, division_number, pool_gameweeks, total_divisions, cfg)

        home_score = match["home"].get("score", 0) or 0
        away_score = match["away"].get("score", 0) or 0
        home_points, home_result = _team_result(home_score, away_score)
        away_points, away_result = _team_result(away_score, home_score)
        home_badges = _extract_badges(match["home"], cfg["trackedBonuses"])
        away_badges = _extract_badges(match["away"], cfg["trackedBonuses"])
        home_sums = _player_sums(match["home"])
        away_sums = _player_sums(match["away"])

        matches_info.append({
            "championshipGameWeekNumber": gw,
            "divisionNumber": division_number,
            "matchId": match.get("id"),
            "status": match.get("status"),
            "finalResult": match.get("finalResult"),
            "home": {
                "userId": match["home"].get("userId"),
                "teamId": match["home"].get("teamId"),
                "score": home_score,
                "points": home_points,
                "points_pond": round(home_points * coeff, 4),
                "result": home_result,
                "badges": home_badges,
                "on_fire": int(home_score - away_score >= 3),
                "manita": int(home_score > away_score and home_score >= 5),
                "cleanSheet": int(away_score == 0),
                "grotaldo": _rotaldo_count(match["home"]) // 3,
                **home_sums,
            },
            "away": {
                "userId": match["away"].get("userId"),
                "teamId": match["away"].get("teamId"),
                "score": away_score,
                "points": away_points,
                "points_pond": round(away_points * coeff, 4),
                "result": away_result,
                "badges": away_badges,
                "on_fire": int(away_score - home_score >= 3),
                "manita": int(away_score > home_score and away_score >= 5),
                "cleanSheet": int(home_score == 0),
                "grotaldo": _rotaldo_count(match["away"]) // 3,
                **away_sums,
            },
        })
    return matches_info


def aggregate_standings(matches_info: list[dict]) -> list[dict]:
    """Cumule une ou plusieurs journées (déjà passées par `compute_matchday_standings`)
    en classement par manager, trié par points pondérés puis différence de buts."""
    standings: dict[str, dict] = {}
    for match in matches_info:
        for side, other in (("home", "away"), ("away", "home")):
            team = match[side]
            opp = match[other]
            uid = team["userId"]
            row = standings.setdefault(uid, {
                "userId": uid,
                "teamName": team.get("teamName", ""),
                "points": 0,
                "points_pond": 0.0,
                "victoires": 0,
                "nuls": 0,
                "defaites": 0,
                "buts_pour": 0,
                "buts_contre": 0,
                "matches_joues": 0,
                "on_fire": 0,
                "manita": 0,
                "cleanSheet": 0,
                "grotaldo": 0,
                "owngoals": 0,
                "Ycards": 0,
                "Rcards": 0,
                "mpggoals": 0,
                "badge_counts": {},
            })
            row["points"] += team["points"]
            row["points_pond"] += team["points_pond"]
            row["victoires"] += int(team["result"] == "V")
            row["nuls"] += int(team["result"] == "N")
            row["defaites"] += int(team["result"] == "D")
            row["buts_pour"] += team["score"]
            row["buts_contre"] += opp["score"]
            row["matches_joues"] += 1
            row["on_fire"] += team["on_fire"]
            row["manita"] += team["manita"]
            row["cleanSheet"] += team["cleanSheet"]
            row["grotaldo"] += team["grotaldo"]
            row["owngoals"] += team["owngoals"]
            row["Ycards"] += team["ycards"]
            row["Rcards"] += team["rcards"]
            row["mpggoals"] += team["mpggoals"]
            for badge_key, present in team["badges"].items():
                if present:
                    row["badge_counts"][badge_key] = row["badge_counts"].get(badge_key, 0) + 1

    ranked = sorted(
        standings.values(),
        key=lambda r: (-r["points_pond"], -(r["buts_pour"] - r["buts_contre"]), -r["buts_pour"]),
    )
    for i, row in enumerate(ranked, start=1):
        row["rang"] = i
    return ranked
