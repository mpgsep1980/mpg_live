"""
Bonus "internes" (par division, au sein d'une ligue) appliqués au classement
cumulé de saison : Pichichi (meilleure attaque), Le Mur (meilleure défense),
Bonus_Champion / Bonus_Second / Bonus_Dernier / Bonus_Podium (classement de
division), Bonus_Boss_Saison (progression saison après saison).

Réplique MPG_Ligue_Camembert_Test_2025.ipynb (cellules 15-16), sans dépendance
à Jupyter — configurable via League_Codes.json -> ligues[].scoring.internalBonuses
(édité depuis le mode admin), avec repli sur les valeurs actuelles si absent.

Hors scope pour l'instant : "Précieux"/Precious_Count (dépend du champ
afterTargetMan du calendrier MPG, une source de données distincte du scoring).

Entrée attendue : `standings`, la sortie de core.scoring.aggregate_standings()
pour UNE division (liste de dicts avec points/buts_pour/buts_contre/...).
"""

# Valeurs par défaut — identiques à ce qui est en dur dans les cellules 15/16.
# Clés de dict en string pour rester JSON-friendly (édition depuis l'admin).
DEFAULT_INTERNAL_BONUS_CONFIG = {
    "pichichiMaxPoints": 3,
    "murMaxPoints": 3,
    "championMaxPoints": 10,
    "secondPoints": 1,
    "dernierPoints": 1,
    "podiumRules": {
        "6": [{"ranks": [1, 2], "points": 2}, {"ranks": [3], "points": 1}],
        "10": [{"ranks": [1, 2], "points": 2}, {"ranks": [3, 4], "points": 1}],
        "14": [{"ranks": [1, 2, 3], "points": 2}, {"ranks": [4, 5], "points": 1}],
        "18": [{"ranks": [1, 2, 3], "points": 2}, {"ranks": [4, 5, 6], "points": 1}],
    },
    "bossSaisonTables": {
        "48": {"start": 4, "subsequent": 10},
        "72": {"start": 2, "subsequent": 10},
        "96": {"start": 2, "subsequent": 10},
    },
    "enabled": {
        "pichichi": True, "mur": True, "champion": True,
        "second": True, "dernier": True, "podium": True, "bossSaison": True,
    },
}


def _match_side(match: dict, user_id: str) -> tuple[bool, int, int] | None:
    """(est_a_domicile, buts_marques, buts_encaisses) pour ce manager dans ce match,
    ou None s'il n'y joue pas."""
    home, away = match["home"], match["away"]
    if home.get("userId") == user_id:
        return True, home.get("score", 0) or 0, away.get("score", 0) or 0
    if away.get("userId") == user_id:
        return False, away.get("score", 0) or 0, home.get("score", 0) or 0
    return None


def _away_goals(user_id: str, division_matches: list[dict]) -> int:
    """Somme des buts marqués par ce manager dans les matchs où IL jouait à
    l'extérieur -- critère INDIVIDUEL (retour utilisateur 2026-08-20), pas limité à
    la confrontation directe : deux managers qui ne se sont jamais affrontés
    l'utilisent quand même (cf. exemple Mouss/Baptiste, Division 9 Liga_Tapas J1 --
    Baptiste 3 buts à l'extérieur, Mouss 0, jamais joué l'un contre l'autre)."""
    total = 0
    for match in division_matches:
        side = _match_side(match, user_id)
        if side and not side[0]:
            total += side[1]
    return total


def _head_to_head_stats(user_ids: set, division_matches: list[dict]) -> dict[str, dict]:
    """{userId: {"points": int, "diff": int}} -- calculés UNIQUEMENT à partir des
    matchs où les DEUX équipes appartiennent à `user_ids` (confrontation directe
    entre les seuls membres du groupe à égalité, retour utilisateur 2026-08-20,
    règle MPG niveaux 4-5 : points de la confrontation directe, PUIS différence de
    buts particulière -- 3/1/0 comme le barème officiel, indépendant de tout
    coefficient de pondération)."""
    stats = {uid: {"points": 0, "diff": 0} for uid in user_ids}
    for match in division_matches:
        home, away = match["home"], match["away"]
        h_uid, a_uid = home.get("userId"), away.get("userId")
        if h_uid not in user_ids or a_uid not in user_ids:
            continue
        h_score, a_score = home.get("score", 0) or 0, away.get("score", 0) or 0
        stats[h_uid]["diff"] += h_score - a_score
        stats[a_uid]["diff"] += a_score - h_score
        if h_score > a_score:
            stats[h_uid]["points"] += 3
        elif h_score < a_score:
            stats[a_uid]["points"] += 3
        else:
            stats[h_uid]["points"] += 1
            stats[a_uid]["points"] += 1
    return stats


def _resolve_tied_group(group: list[dict], division_matches: list[dict] | None) -> list[dict]:
    """Reclasse un groupe d'équipes à ÉGALITÉ PARFAITE sur points/diff/buts_pour
    (les 3 premiers niveaux du départage MPG), selon les niveaux 4-6 : confrontation
    directe (points puis diff, entre les SEULS membres du groupe), puis buts marqués
    à l'extérieur (individuel). Groupe inchangé (ordre stable, hérité du tri
    précédent) si `division_matches` absent, ou si l'égalité persiste malgré ces 3
    niveaux -- MPG ne précise rien au-delà (retour utilisateur 2026-08-20)."""
    if len(group) < 2 or not division_matches:
        return group
    user_ids = {t["userId"] for t in group}
    h2h = _head_to_head_stats(user_ids, division_matches)
    return sorted(
        group,
        key=lambda t: (
            h2h[t["userId"]]["points"],
            h2h[t["userId"]]["diff"],
            _away_goals(t["userId"], division_matches),
        ),
        reverse=True,
    )


def _rank_teams(standings: list[dict], division_matches: list[dict] | None = None) -> list[dict]:
    """Classement de division/poule -- règle MPG officielle (retour utilisateur
    2026-08-20, DISTINCTE du départage à 6 critères du Super Classement des 72
    managers -- cf. core/live_projection.py, qui utilise Ycards/Rcards : NE PAS
    mélanger les deux, ce sont deux classements différents avec des règles
    différentes) :
    1. points bruts (non pondérés)  2. différence de buts générale
    3. meilleure attaque (buts pour)
    puis, ENTRE LES SEULES équipes encore à égalité sur ces 3 critères :
    4. points en confrontation directe  5. différence de buts particulière
    6. buts marqués à l'extérieur

    Avant ce correctif, le tri s'arrêtait à un 4e critère redondant (buts contre --
    mathématiquement déjà fixé dès que diff ET buts pour sont égaux, donc ne
    départageait jamais rien en pratique) : toute égalité restante retombait sur
    l'ordre d'itération, l'équipe à domicile passant systématiquement devant (4
    cas vérifiés sur Liga_Tapas J1, toujours dans ce sens). `division_matches` :
    TOUS les matchs ayant produit `standings` (une ou plusieurs journées) --
    nécessaire pour les niveaux 4-6 ; absent = repli sur les 3 premiers niveaux
    seulement (comportement précédent, connu incomplet)."""
    primary_sorted = sorted(
        standings,
        key=lambda t: (t["points"], t["buts_pour"] - t["buts_contre"], t["buts_pour"]),
        reverse=True,
    )
    result = []
    i = 0
    while i < len(primary_sorted):
        key_i = (primary_sorted[i]["points"], primary_sorted[i]["buts_pour"] - primary_sorted[i]["buts_contre"], primary_sorted[i]["buts_pour"])
        j = i + 1
        while j < len(primary_sorted):
            key_j = (primary_sorted[j]["points"], primary_sorted[j]["buts_pour"] - primary_sorted[j]["buts_contre"], primary_sorted[j]["buts_pour"])
            if key_j != key_i:
                break
            j += 1
        result.extend(_resolve_tied_group(primary_sorted[i:j], division_matches))
        i = j
    return result


def compute_internal_bonuses(
    standings: list[dict],
    division_number: int,
    current_gameweek: int,
    last_gameweek: int,
    league_size: int,
    season_number: int,
    season_start: int,
    league_name: str,
    config: dict | None = None,
    division_matches: list[dict] | None = None,
) -> list[dict]:
    """Mute et retourne `standings` — ajoute standings[i]["bonus_details"][...]
    pour chaque bonus interne activé. `standings` doit couvrir une seule division
    (sortie de core.scoring.aggregate_standings pour cette division).
    `division_matches` : TOUS les matchs bruts ayant produit `standings` (toutes
    journées confondues) -- transmis tel quel à _rank_teams pour les niveaux 4-6
    du départage MPG (confrontation directe, buts à l'extérieur). Absent = repli
    documenté sur les 3 premiers niveaux seulement, cf. _rank_teams."""
    cfg = config or DEFAULT_INTERNAL_BONUS_CONFIG
    enabled = cfg.get("enabled", {})

    for t in standings:
        t.setdefault("bonus_details", {})

    if not standings:
        return standings

    ranked = _rank_teams(standings, division_matches)
    scale = (current_gameweek / last_gameweek) if last_gameweek else 0

    # Arrondi a 1 decimale pour les 3 bonus au PRORATA (scale) -- retour
    # utilisateur 2026-08-18, revu le meme jour ("un simple arrondi a une
    # decimale serait deja tres bien", apres un premier passage a 4
    # decimales puis une proposition de table par paliers de 0.25 --
    # simplifie a round(x, 1) au final). Bonus_Second/Bonus_Dernier/
    # Bonus_Podium ne sont PAS concernes -- valeurs fixes (table/config),
    # jamais multipliees par `scale`.
    if enabled.get("pichichi", True):
        max_score_plus = max(t["buts_pour"] for t in standings)
        for t in standings:
            t["bonus_details"]["Pichichi"] = round(cfg["pichichiMaxPoints"] * scale, 1) if t["buts_pour"] == max_score_plus else 0

    if enabled.get("mur", True):
        min_score_minus = min(t["buts_contre"] for t in standings)
        for t in standings:
            t["bonus_details"]["Le_Mur"] = round(cfg["murMaxPoints"] * scale, 1) if t["buts_contre"] == min_score_minus else 0

    if enabled.get("champion", True):
        for t in standings:
            is_champion = t is ranked[0]
            t["bonus_details"]["Bonus_Champion"] = round(cfg["championMaxPoints"] * scale, 1) if is_champion else 0
            if is_champion:
                # Division remportée -- necessaire pour Multi Boss (core/multi_boss.py),
                # qui ne peut pas se contenter des points Bonus_Champion/Boss_Saison
                # (son bareme est indexe par division, pas par les points deja attribues).
                # Champion_Division (sans suffixe) reste pour la saison EN COURS (lu par
                # _finalize_league_row -- retour utilisateur 2026-08-12 : doit retomber a
                # 0/disparaitre si plus champion cette journee-la). Champion_Division_
                # {season_number} est la version qui SURVIT une fois la saison archivee et
                # une nouvelle commencee (comme Boss_Saison_{season}) -- retour utilisateur
                # 2026-08-18 : "le titre de poules doit etre conserve dans les stats" --
                # sans ce suffixe, le titre d'une saison passee etait perdu (efface, pas
                # juste ignore) des que ce manager n'etait plus champion lors d'un appel
                # ulterieur, cf. _finalize_league_row.
                t["bonus_details"]["Champion_Division"] = division_number
                t["bonus_details"][f"Champion_Division_{season_number}"] = division_number

    if enabled.get("second", True):
        for t in standings:
            t["bonus_details"]["Bonus_Second"] = cfg["secondPoints"] if len(ranked) > 1 and t is ranked[1] else 0

    if enabled.get("dernier", True):
        for t in standings:
            t["bonus_details"]["Bonus_Dernier"] = cfg["dernierPoints"] if t is ranked[-1] else 0

    if enabled.get("podium", True):
        rules = cfg["podiumRules"].get(str(last_gameweek), [])
        for t in standings:
            t["bonus_details"]["Bonus_Podium"] = 0
        for rank, t in enumerate(ranked, start=1):
            for rule in rules:
                if rank in rule["ranks"]:
                    t["bonus_details"]["Bonus_Podium"] = rule["points"]
                    break

    if enabled.get("bossSaison", True):
        table = cfg["bossSaisonTables"].get(str(league_size))
        if table:
            champion = ranked[0]
            key = f"{league_name}_Boss_Saison_{season_number}"
            bonus_value = None
            if season_number == season_start:
                bonus_value = table["start"]
            elif season_number in (season_start + 1, season_start + 2):
                # Meme valeur quelle que soit la division -- un champion reste champion,
                # que ce soit en D1 ou en D9 (cf. Bonus_Champion, deja flat pour la
                # meme raison).
                bonus_value = table["subsequent"]
            if bonus_value is not None:
                champion["bonus_details"][key] = bonus_value

    return standings
