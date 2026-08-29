"""
Fonction cloud (Flask, hebergement externe -- ex. Render) exposant
compute_division_live_scores avec bonus_choices HYPOTHETIQUES, pour que le
site statique mpg_live (GitHub Pages, aucune capacite de calcul) puisse
proposer aux managers "et si je joue tel coup / mon adversaire joue tel
autre" pendant une journee EN COURS -- retour utilisateur 2026-08-24 :
"a chaque fois qu'une journee commencera, on ajoutera les points
theoriques a l'instant T et on permettra aux managers de faire des
simulations de situations selon les bonus mis ou non, comme developpe
avec lancer_live.bat", puis "regarde bien ce qui a deja ete fait ... tout
est disponible, il faut que tu branches tout".

Port de DEUX endpoints de admin_server.py (mpg_app) -- "Mon Bonus"
(simulate-bonus, un choix manuel) ET "Bonus Adverse -- etat des lieux"
(live-scenario-sweep, balaie AUTOMATIQUEMENT tous les bonus adverses
possibles pour cette taille de division, McDo+ teste sur chaque joueur de
champ). PAS le simulateur de journee acceleree GameweekSimulation, une
fonctionnalite distincte, plus lourde, pas demandee ici. Reutilise
core/live_scoring.py/core/api.py TELS QUELS (aucune reimplementation JS de
la logique de scoring -- decision utilisateur 2026-08-24, "petite fonction
cloud Python").

Contrairement a admin_server.py (local, pas de controle d'acces necessaire
sur 127.0.0.1), ce service est PUBLIC sur internet -- mais reste read-only
(aucune ecriture MPG ni Supabase, juste un calcul hypothetique renvoye au
navigateur) : pas d'authentification portee pour ce premier increment
(LIMITE CONNUE v1, a completer si un jour ce calcul doit rester prive a
CE manager -- admin_server.py utilisait _is_owner via core/auth.py, pas
encore porte).

Lancer en local : python simulate_api/app.py (necessite MPG_TOKEN dans
l'environnement, cf. .env a la racine du repo).
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from flask import Flask, jsonify, request
from supabase import create_client

from core.api import (
    get_division_matches, get_championship_match, get_division_info,
    get_live_total_divisions, get_division_team_names,
    get_division_teams, get_championship_ids, get_player_pool, get_used_bonus_counts,
)
from core.live_scoring import (
    compute_division_live_scores, collect_real_match_ids, VALID_BONUSES, bonus_available_for_division_size,
    bonus_remaining_counts,
)
from core.league import league_from_supabase_row
from core.live_projection import (
    league_setup, resolve_division_rows, resolve_league_wide_ranks_for_simulation,
    finalize_division_data, compute_super_classement,
)
from core.lineup_recommender import recommend_lineup, full_squad, FORMATIONS

app = Flask(__name__)

# Cle anonyme -- deja publique cote client (embarquee telle quelle dans
# CHAQUE page de site/, ex. site/division.html) : la reutiliser ici cote
# serveur n'expose rien de plus, RLS restreint deja l'ecriture au seul role
# service_role (cf. db/schema.sql). Lecture seule (jamais d'ecriture ici) --
# retour utilisateur 2026-08-26, "s'assurer de la simulation des classements
# ... en prenant en compte les resultats de tous les matchs en cours" :
# /simulate-classement a besoin de lire leagues/league_classement_archive/
# division_classement_live pour situer le scenario hypothetique du manager
# parmi le reste des ligues suivies.
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://zvmngpoogwjiknqrkjky.supabase.co")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "sb_publishable_YhAD0_spZJF58Dt_TTQplQ_VrFSKRK8")
_supabase = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)

# Maillots reels MPG, hebergees publiquement par MPG lui-meme -- retour
# utilisateur 2026-08-29, "meme pas l'esthetique avec le demi terrain de
# football et les maillots" (vu dans un projet soeur, MPG_Perso_L1, qui
# stocke sa PROPRE copie base64 des maillots) : l'URL MPG est directement
# hotlinkable (verifie : s3.eu-west-1.amazonaws.com/image.mpg/jersey/
# {saison}/{championshipId}/{clubId numerique}.png repond 200 sans auth).
# La "saison" ici est l'annee calendaire reelle (stats.nextMatch.season du
# pool, ex. 2026), PAS le numero de saison interne a la division (ex. 22)
# utilise ailleurs dans ce fichier -- deux systemes de numerotation
# distincts chez MPG.
#
# MAIS cette URL renvoie 403 (pas 404) pour les clubs sans maillot boutique
# MPG -- verifie : essentiellement toute la Ligue 2 (Troyes, Auxerre,
# Saint-Etienne, Metz, Guingamp, Nantes, Reims, Rodez, Nancy...) et
# quelques autres divisions secondaires. Ces memes maillots existent deja
# LOCALEMENT (C:\Users\sebas\Desktop\Python\Clubs\Jerseys, mis a jour par
# mpg_app/update_jerseys.py depuis /locker-room/extended -- la boutique
# MPG elle-meme) : copies une bonne fois dans site/assets/jerseys/
# {clubId numerique}.png (renommes par id plutot que nom, pour eviter tout
# probleme d'accents/espaces dans l'URL), servies par GitHub Pages comme
# repli cote frontend (onerror sur le <img>, cf. compo.html) si le hotlink
# MPG echoue. Snapshot fige a la copie -- un futur club promu/jamais vu
# n'aura ni l'un ni l'autre, le <img> disparait alors silencieusement.
JERSEY_BASE_URL = "https://s3.eu-west-1.amazonaws.com/image.mpg/jersey"
JERSEY_FALLBACK_BASE_URL = "https://mpgsep1980.github.io/mpg_live/assets/jerseys"


def jersey_urls_for(pool_entry: dict, champ_id: int) -> tuple[str | None, str | None]:
    club_id = pool_entry.get("clubId") or ""
    club_num = club_id.rsplit("_", 1)[-1] if club_id else None
    season = ((pool_entry.get("stats") or {}).get("nextMatch") or {}).get("season")
    primary = f"{JERSEY_BASE_URL}/{season}/{champ_id}/{club_num}.png" if club_num and season else None
    fallback = f"{JERSEY_FALLBACK_BASE_URL}/{club_num}.png" if club_num else None
    return primary, fallback


@app.after_request
def _add_cors_headers(response):
    # Site public (GitHub Pages) -- pas de session/cookie ici, un
    # Access-Control-Allow-Origin ouvert n'expose rien de plus que ce que
    # l'API MPG elle-meme accepte deja de rendre public (donnees de match).
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    return response


class _BadRequest(Exception):
    def __init__(self, message: str, status: int = 400):
        self.message = message
        self.status = status


def _ensure_user_ids(division_matches: list[dict], short_id: str, season: int, division: int) -> None:
    """Avant le coup d'envoi de la journee (statut MPG "pas commence"),
    get_division_matches renvoie home/away["userId"] = null -- seul teamId
    est peuple, MPG ne verrouille l'association manager<->match qu'au coup
    d'envoi (verifie reel : Rosbeef_League D8 J2, status=0, userId null des
    les deux cotes de chacun des 3 matchs). Reconstitue userId depuis teamId
    via usersTeams (core/api.py::get_division_info, disponible quel que soit
    l'etat du match) pour que TOUT le reste (recherche du match du manager,
    determination de l'adversaire, compute_division_live_scores) continue de
    fonctionner sans aucun autre changement -- retour utilisateur 2026-08-25,
    "on doit absolument la blinder avant le debut des matchs" : simulate-bonus
    renvoyait "Match introuvable pour ce manager dans cette division" pour
    TOUTE journee pas encore commencee, ce qui est precisement le moment ou
    un manager veut planifier son coup. Mute `division_matches` en place ;
    ne fait l'appel reseau supplementaire que si necessaire (journee deja en
    cours/terminee = userId deja peuple, aucun cout ajoute)."""
    if all(m["home"].get("userId") and m["away"].get("userId") for m in division_matches):
        return
    users_teams = get_division_info(short_id, season, division).get("usersTeams", {}) or {}
    team_to_user = {team_id: user_id for user_id, team_id in users_teams.items()}
    for m in division_matches:
        for side in ("home", "away"):
            if not m[side].get("userId"):
                m[side]["userId"] = team_to_user.get(m[side].get("teamId"))


def _load_match_context(args, require_own_bonus_valid: bool = True):
    """Commun a simulate-bonus et live-scenario-sweep : resout le match du
    manager + son adversaire + la taille de division + les vrais matchs
    (un seul fetch reseau par vrai match, partage par tous les scenarios
    recalcules ensuite EN MEMOIRE -- meme principe que admin_server.py::
    api_live_scenario_sweep, "en UN SEUL fetch reseau"). Leve _BadRequest
    (jamais None) pour que chaque route garde son propre message d'erreur
    exact cote appelant."""
    short_id = args.get("shortId")
    season = args.get("season")
    division = args.get("division")
    game_week = args.get("gameweek")
    user_id = args.get("userId")
    own_bonus = args.get("ownBonus") or None

    if not all([short_id, season, division, game_week, user_id]):
        raise _BadRequest("parametres manquants")
    if own_bonus and own_bonus not in VALID_BONUSES:
        raise _BadRequest(f"Bonus inconnu ou pas encore supporte en live : {own_bonus}")

    division_matches = get_division_matches(short_id, int(season), int(division), int(game_week))
    _ensure_user_ids(division_matches, short_id, int(season), int(division))
    div_match = next(
        (m for m in division_matches if user_id in (m["home"].get("userId"), m["away"].get("userId"))), None,
    )
    if not div_match:
        raise _BadRequest("Match introuvable pour ce manager dans cette division", 404)
    # Compositions pas encore verrouillees par MPG (players/playersOnPitch
    # vides tant que le coup d'envoi n'est pas proche, meme apres le
    # repli _ensure_user_ids ci-dessus qui ne resout que teamId->userId) :
    # echec propre plutot qu'un KeyError/500 dans collect_real_match_ids
    # (retour utilisateur 2026-08-25, "on doit absolument la blinder avant
    # le debut des matchs" -- verifie reel sur une journee a venir : home/
    # away n'ont alors que teamId/composition/playersOnPitch={}).
    if not div_match["home"].get("players") or not div_match["away"].get("players"):
        raise _BadRequest(
            "Compositions pas encore disponibles pour cette journee -- reessaie juste avant le coup d'envoi.",
            409,
        )
    opponent_user_id = (
        div_match["away"]["userId"] if div_match["home"].get("userId") == user_id else div_match["home"]["userId"]
    )

    division_size = len(division_matches) * 2
    if require_own_bonus_valid and own_bonus and not bonus_available_for_division_size(own_bonus, division_size):
        raise _BadRequest(f"{own_bonus} n'existe pas pour une division de {division_size} (tableau officiel MPG)")

    match_ids = collect_real_match_ids([div_match])
    real_matches_by_id = {mid: get_championship_match(mid) for mid in match_ids}

    return {
        "div_match": div_match, "user_id": user_id, "opponent_user_id": opponent_user_id,
        "division_size": division_size, "real_matches_by_id": real_matches_by_id, "own_bonus": own_bonus,
    }


def _load_division_context(args):
    """Variante de _load_match_context pour /simulate-classement : conserve
    TOUS les matchs de la division (pas seulement celui du manager) et fetch
    les vrais matchs de TOUTE la division -- resolve_division_rows a besoin
    du resultat des AUTRES matchs de la division, pas seulement du sien,
    pour recalculer un classement de division complet (retour utilisateur
    2026-08-26, "en prenant en compte les resultats de tous les matchs en
    cours"). Leve _BadRequest (jamais None), meme contrat que
    _load_match_context."""
    short_id = args.get("shortId")
    season = args.get("season")
    division = args.get("division")
    game_week = args.get("gameweek")
    user_id = args.get("userId")
    own_bonus = args.get("ownBonus") or None
    opponent_bonus = args.get("opponentBonus") or None
    target_player_id = args.get("targetPlayerId") or None

    if not all([short_id, season, division, game_week, user_id]):
        raise _BadRequest("parametres manquants")
    if own_bonus and own_bonus not in VALID_BONUSES:
        raise _BadRequest(f"Bonus inconnu ou pas encore supporte en live : {own_bonus}")
    if opponent_bonus and opponent_bonus not in VALID_BONUSES:
        raise _BadRequest(f"Bonus inconnu ou pas encore supporte en live : {opponent_bonus}")

    season, division, game_week = int(season), int(division), int(game_week)

    division_matches = get_division_matches(short_id, season, division, game_week)
    _ensure_user_ids(division_matches, short_id, season, division)
    div_match = next(
        (m for m in division_matches if user_id in (m["home"].get("userId"), m["away"].get("userId"))), None,
    )
    if not div_match:
        raise _BadRequest("Match introuvable pour ce manager dans cette division", 404)
    if not div_match["home"].get("players") or not div_match["away"].get("players"):
        raise _BadRequest(
            "Compositions pas encore disponibles pour cette journee -- reessaie juste avant le coup d'envoi.",
            409,
        )
    opponent_user_id = (
        div_match["away"]["userId"] if div_match["home"].get("userId") == user_id else div_match["home"]["userId"]
    )

    division_size = len(division_matches) * 2
    if own_bonus and not bonus_available_for_division_size(own_bonus, division_size):
        raise _BadRequest(f"{own_bonus} n'existe pas pour une division de {division_size} (tableau officiel MPG)")
    if opponent_bonus and not bonus_available_for_division_size(opponent_bonus, division_size):
        raise _BadRequest(f"{opponent_bonus} n'existe pas pour une division de {division_size} (tableau officiel MPG)")

    match_ids = collect_real_match_ids(division_matches)
    real_matches_by_id = {mid: get_championship_match(mid) for mid in match_ids}

    league_row = _supabase.table("leagues").select("*").eq("code", short_id).limit(1).execute().data
    if not league_row:
        raise _BadRequest(f"Ligue {short_id} introuvable en base (jamais suivie par mpg_live)", 404)
    league = league_from_supabase_row(league_row[0])

    return {
        "short_id": short_id, "season": season, "division": division, "game_week": game_week,
        "division_matches": division_matches, "real_matches_by_id": real_matches_by_id,
        "user_id": user_id, "opponent_user_id": opponent_user_id,
        "own_bonus": own_bonus, "opponent_bonus": opponent_bonus, "target_player_id": target_player_id,
        "league": league,
    }


@app.route("/simulate-classement", methods=["GET", "OPTIONS"])
def simulate_classement():
    """Impact PROJETE sur le classement (division/rang de ligue/Super
    Classement) du scenario hypothetique deja teste par /simulate-bonus --
    retour utilisateur 2026-08-26, "s'assurer de la simulation des
    classements (divisions/ligues/Super Classement) en prenant en compte les
    resultats de tous les matchs en cours dans les differents championnats".
    Prolonge /simulate-bonus (qui ne renvoyait que le score du match testé)
    sans le remplacer -- memes parametres (ownBonus/targetPlayerId/
    opponentBonus optionnels, absents = classement REEL en cours, aucun
    scenario).

    Recalcule EN MEMOIRE la division entiere du manager (resolve_division_rows,
    memes fonctions que scripts/live_job.py) avec son match hypothetique,
    puis situe ce resultat parmi TOUTES LES AUTRES divisions/ligues telles
    qu'ecrites par le DERNIER tick live (resolve_league_wide_ranks_for_
    simulation / compute_super_classement override) -- donc deja a jour des
    resultats de tous les autres matchs en cours au meme instant, dans cette
    ligue comme dans les autres. N'ecrit jamais rien sur Supabase (purement
    un calcul hypothetique renvoye au navigateur)."""
    if request.method == "OPTIONS":
        return "", 204

    try:
        ctx = _load_division_context(request.args)
    except _BadRequest as e:
        return jsonify({"error": e.message}), e.status

    bonus_choices = {}
    if ctx["own_bonus"]:
        bonus_choices[ctx["user_id"]] = {"bonus": ctx["own_bonus"], "targetPlayerId": ctx["target_player_id"]}
    if ctx["opponent_bonus"]:
        bonus_choices[ctx["opponent_user_id"]] = {"bonus": ctx["opponent_bonus"], "targetPlayerId": None}

    results = compute_division_live_scores(ctx["division_matches"], ctx["real_matches_by_id"], bonus_choices)

    league, division, game_week = ctx["league"], ctx["division"], ctx["game_week"]
    try:
        total_divisions = get_live_total_divisions(ctx["short_id"])
    except Exception:
        return jsonify({"error": "totalDivisions introuvable sur /dashboard MPG -- reessaie plus tard."}), 502

    setup = league_setup(_supabase, league, [division], game_week)
    own_rows, _is_live = resolve_division_rows(league, division, results, game_week, total_divisions, setup)

    try:
        team_names = get_division_team_names(ctx["short_id"], ctx["season"], division)
    except Exception:
        team_names = {}

    league_ranks = resolve_league_wide_ranks_for_simulation(
        _supabase, league, division, own_rows, setup["match_bonus_cfg"],
    )
    own_division_data = finalize_division_data(own_rows, league_ranks, team_names)

    my_entry = next((e for e in own_division_data if e["userId"] == ctx["user_id"]), None)
    if my_entry is None:
        return jsonify({"error": "Impossible de resoudre le manager dans le classement recalcule."}), 500

    ranked_super = compute_super_classement(_supabase, {(ctx["short_id"], division): own_division_data})
    my_super = next((r for r in ranked_super if r["userId"] == ctx["user_id"]), None)

    return jsonify({
        "division": {"rang": my_entry["rang"], "points": my_entry["points"], "size": len(own_division_data)},
        # "rang"/"points" partout (retour utilisateur 2026-08-27, teste
        # interactivement) -- "league" utilisait a tort rangLigue/pointsLigue,
        # different de "division"/"superClassement", ce qui rendait le rang
        # de ligue silencieusement "undefined" cote site (classementItemHtml,
        # site/match.html et site/simulations.html, lit current.rang/.points
        # de facon uniforme sur les trois sections).
        "league": {
            "rang": my_entry.get("rang_ligue"), "points": my_entry.get("points_ligue"),
            "size": len(league_ranks),
        },
        "superClassement": (
            {"rang": my_super["rang"], "points": my_super["points"], "size": len(ranked_super)}
            if my_super else None
        ),
    })


@app.route("/simulate-bonus", methods=["GET", "OPTIONS"])
def simulate_bonus():
    """Reproduit tel quel admin_server.py::api_simulate_my_bonus (mpg_app) --
    memes parametres, meme contrat. Params (query string) :
    shortId, season, division, gameweek, userId (obligatoires),
    ownBonus/targetPlayerId/opponentBonus (optionnels -- le coup HYPOTHETIQUE
    a tester, pas necessairement celui reellement declare sur MPG). Appele
    SANS aucun bonus, sert aussi a recuperer les compositions completes
    (players/out/bench avec noms reels) pour construire le selecteur de
    cible cote site -- meme reponse brute que compute_division_live_scores,
    rien de retire.
    Renvoie le match recalcule (home/away, "score", "total", badges...)
    exactement comme s'il avait ete joue avec ces choix -- ne modifie RIEN
    sur MPG ni Supabase, purement un calcul en memoire."""
    if request.method == "OPTIONS":
        return "", 204

    target_player_id = request.args.get("targetPlayerId") or None
    opponent_bonus = request.args.get("opponentBonus") or None
    if opponent_bonus and opponent_bonus not in VALID_BONUSES:
        return jsonify({"error": f"Bonus inconnu ou pas encore supporte en live : {opponent_bonus}"}), 400

    try:
        ctx = _load_match_context(request.args)
    except _BadRequest as e:
        return jsonify({"error": e.message}), e.status

    if opponent_bonus and not bonus_available_for_division_size(opponent_bonus, ctx["division_size"]):
        return jsonify({"error": f"{opponent_bonus} n'existe pas pour une division de {ctx['division_size']} (tableau officiel MPG)"}), 400

    bonus_choices = {}
    if ctx["own_bonus"]:
        bonus_choices[ctx["user_id"]] = {"bonus": ctx["own_bonus"], "targetPlayerId": target_player_id}
    if opponent_bonus:
        bonus_choices[ctx["opponent_user_id"]] = {"bonus": opponent_bonus, "targetPlayerId": None}

    match = compute_division_live_scores([ctx["div_match"]], ctx["real_matches_by_id"], bonus_choices)[0]
    return jsonify(match)


@app.route("/live-scenario-sweep", methods=["GET", "OPTIONS"])
def live_scenario_sweep():
    """Reproduit tel quel admin_server.py::api_live_scenario_sweep -- etat
    des lieux complet des coups adverses possibles (Zahia/Suarez/Cheat Code/
    Tonton Pat'/Valise a Nanard/Miroir, filtre par taille de division, plus
    McDo+ teste sur CHAQUE joueur de champ adverse -- jamais le gardien) en
    UN SEUL fetch reseau. ownBonus/targetPlayerId (mon coup, optionnel)
    reste fixe sur tous les scenarios balayes. Renvoie {baseline, scenarios}
    -- scenarios tries du plus defavorable au plus favorable pour moi
    (deltaMine - deltaTheirs)."""
    if request.method == "OPTIONS":
        return "", 204

    own_target_player_id = request.args.get("targetPlayerId") or None

    try:
        ctx = _load_match_context(request.args)
    except _BadRequest as e:
        return jsonify({"error": e.message}), e.status

    user_id, opponent_user_id = ctx["user_id"], ctx["opponent_user_id"]
    div_match, real_matches_by_id, division_size = ctx["div_match"], ctx["real_matches_by_id"], ctx["division_size"]
    own_bonus = ctx["own_bonus"]

    def compute_match(bonus_choices):
        return compute_division_live_scores([div_match], real_matches_by_id, bonus_choices)[0]

    def summarize(match):
        mine = match["home"] if match["home"]["userId"] == user_id else match["away"]
        theirs = match["away"] if mine is match["home"] else match["home"]
        return {"myScore": mine["score"], "theirScore": theirs["score"]}

    base_bonus_choices = {}
    if own_bonus:
        base_bonus_choices[user_id] = {"bonus": own_bonus, "targetPlayerId": own_target_player_id}

    baseline_match = compute_match(dict(base_bonus_choices))
    baseline = summarize(baseline_match)
    opponent_team = baseline_match["home"] if baseline_match["home"]["userId"] == opponent_user_id else baseline_match["away"]
    # Exclut les joueurs entres en cours de match (replaced_starter renseigne
    # -- cf. core/live_scoring.py, le slot affiche desormais SON remplacant,
    # pas le titulaire d'origine) : retour utilisateur 2026-08-25, "tu
    # appliques des bonus sur des joueurs qui sont remplacants au debut du
    # match" -- un manager choisit sa cible McDo+ AVANT le coup d'envoi,
    # seuls les titulaires DECLARES a ce moment-la sont des cibles plausibles
    # pour l'adversaire (un remplacant entre ensuite n'a jamais pu etre vise
    # a l'avance). Le titulaire D'ORIGINE remplace reste, lui, une cible
    # valable -- reintroduit depuis opponent_team["out"], meme filtre
    # is_default_rating que site/match.html::targetSelectHtml (son vrai
    # match pas encore joue : un bonus choisi maintenant n'a alors jamais
    # pu influencer une decision deja reelle, cf. commentaire de cette
    # fonction cote client).
    out_originals = [
        (p["playerId"], p["name"]) for p in opponent_team.get("out", [])
        if p.get("is_default_rating")
    ]
    outfield_players = [
        (pid, p["name"]) for pid, p in opponent_team["players"].items()
        if p["position"] != 1 and not p.get("replaced_starter")
    ] + out_originals

    def scenario_row(bonus_key, label, target_player_id=None, target_name=None):
        choices = dict(base_bonus_choices)
        choices[opponent_user_id] = {"bonus": bonus_key, "targetPlayerId": target_player_id}
        match = compute_match(choices)
        result = summarize(match)

        # Si je joue Miroir et que ce scenario teste un McDo+ adverse, mon
        # Miroir le vole -- indique QUI chez moi en beneficie (meme numero de
        # slot que la cible adverse, cf. resolve_match_bonus_effects), sinon
        # invisible/pas verifiable sans regarder le plateau.
        display_label = label
        if own_bonus == "mirror" and bonus_key == "boostOnePlayer":
            mine = match["home"] if match["home"]["userId"] == user_id else match["away"]
            beneficiary = next((p["name"] for p in mine["players"].values() if p.get("bonus_tag") == "boostOnePlayer"), None)
            if beneficiary:
                display_label = f"{label} (chez moi : {beneficiary})"

        return {
            "opponentBonus": bonus_key, "label": display_label,
            "targetPlayerId": target_player_id, "targetName": target_name,
            **result,
            "deltaMine": round(result["myScore"] - baseline["myScore"], 2),
            "deltaTheirs": round(result["theirScore"] - baseline["theirScore"], 2),
        }

    # Seuls les bonus qui EXISTENT pour cette taille de division sont
    # balayes (tableau officiel MPG) -- inutile/trompeur de tester un
    # Miroir dans une division de 4 ou il n'est pas encore alloue.
    all_types = [
        ("boostAllPlayers", "Zahia"),
        ("nerfGoalkeeper", "Suarez"),
        ("nerfAllPlayers", "Cheat Code"),
        ("blockTacticalSubs", "Tonton Pat'"),
        ("removeGoal", "Valise à Nanard"),
        ("mirror", "Miroir"),
    ]
    scenarios = [
        scenario_row(bonus_key, label)
        for bonus_key, label in all_types
        if bonus_available_for_division_size(bonus_key, division_size)
    ]
    if bonus_available_for_division_size("boostOnePlayer", division_size):
        scenarios.extend(
            scenario_row("boostOnePlayer", f"McDo+ sur {name}", pid, name)
            for pid, name in outfield_players
        )
    # Trie par swing net de difference de buts (le plus defavorable pour moi
    # en premier) -- Suarez/Cheat Code se manifestent surtout via deltaTheirs
    # (le gardien ne marque jamais lui-meme), deltaMine seul serait trompeur.
    scenarios.sort(key=lambda s: s["deltaMine"] - s["deltaTheirs"])

    return jsonify({"baseline": baseline, "scenarios": scenarios})


@app.route("/recommend-lineup", methods=["GET", "OPTIONS"])
def recommend_lineup_route():
    """Recommandation de composition (XI/banc/capitaine) pour l'onglet "Compo"
    -- retour utilisateur 2026-08-27, "serait-il possible de donner des
    instructions a l'appli pour faire une compo ... et de l'envoyer vers
    MPG pour qu'elle se fasse seule". Decision explicite APRES discussion
    (risque CGU d'automatiser une action de jeu sur un tiers, cf. plan) :
    ce endpoint ne fait QUE calculer/afficher, jamais ecrire sur MPG -- au
    manager de recopier lui-meme la recommandation dans la vraie appli.

    Params (query string) : shortId, season, division, userId. PAS de
    gameweek -- l'effectif possede n'est pas specifique a une journee,
    contrairement aux autres endpoints de ce fichier.

    Renvoie {formation, starters: [{playerId, name, position, projection}],
    bench: [...], captainId, squad: [...], formations: {...}}. `squad`
    contient TOUS les joueurs possedes (y compris sans projection fiable,
    donc `projection: null`) et `formations` le tableau des formations
    valides -- pour que le frontend permette d'editer la recommandation a
    la main (retour utilisateur 2026-08-29, apres premier test reel :
    "attention au statut blesse des joueurs ... les joueurs sans notes
    n'apparaissent pas ... il faut laisser la possibilite de modifier la
    formation et les joueurs" -- confirme aussi que le pool MPG n'expose
    aucun champ blessure/suspension, cf. docstring core/lineup_
    recommender.py, donc la recommandation reste une proposition de depart
    a corriger manuellement, jamais une decision finale). `formation`/
    `starters`/`bench`/`captainId` restent `null`/`[]` (pas 409) si aucune
    formation n'est automatiquement realisable -- le manager peut quand
    meme construire sa compo a la main a partir de `squad`. `bonusRemaining`
    (retour utilisateur 2026-08-29, "ne proposer un bonus a utiliser que
    s'il reste encore disponible" -- puis correction le meme jour, "on en a
    2 de base dans une ligue de 6" pour McDo+, PAS 1) : {bonus: nombre
    d'utilisations encore disponibles cette saison}, cf. core.live_scoring.
    bonus_remaining_counts (tableau officiel MPG des quantites par taille
    de poule) et core.api.get_used_bonus_counts (occurrences deja lancees,
    scanne les journees deja jouees) -- retombe sur les quantites pleines
    (jamais decrementees) plutot que de faire echouer toute la
    recommandation si ce calcul echoue."""
    if request.method == "OPTIONS":
        return "", 204

    short_id = request.args.get("shortId")
    season = request.args.get("season")
    division = request.args.get("division")
    user_id = request.args.get("userId")
    if not all([short_id, season, division, user_id]):
        return jsonify({"error": "parametres manquants"}), 400
    season, division = int(season), int(division)

    info = get_division_info(short_id, season, division)
    team_id = (info.get("usersTeams") or {}).get(user_id)
    if not team_id:
        return jsonify({"error": "Manager introuvable dans cette division"}), 404

    teams = get_division_teams(short_id, season, division)
    team = next((t for t in teams if t.get("id") == team_id), None)
    if not team:
        return jsonify({"error": "Equipe introuvable dans cette division"}), 404
    squad_ids = list((team.get("squad") or {}).keys())

    champ_ids = get_championship_ids()
    champ_id = champ_ids.get(short_id)
    if champ_id is None:
        return jsonify({"error": "Championnat introuvable sur le dashboard MPG"}), 502
    pool = get_player_pool(champ_id)

    def resolve(entry):
        p = pool.get(entry["playerId"], {})
        name = f"{p.get('firstName', '')} {p.get('lastName', '')}".strip()
        jersey_url, jersey_fallback_url = jersey_urls_for(p, champ_id)
        return {**entry, "name": name, "jerseyUrl": jersey_url, "jerseyFallbackUrl": jersey_fallback_url}

    rec = recommend_lineup(squad_ids, pool)
    squad = [resolve(p) for p in full_squad(squad_ids, pool)]

    try:
        used_counts = get_used_bonus_counts(short_id, season, division, user_id)
        bonus_remaining = bonus_remaining_counts(len(teams), used_counts)
    except Exception:
        # Non bloquant -- retour utilisateur 2026-08-29 : ce n'est qu'un
        # affinage du picker de bonus, pas indispensable au reste de la
        # recommandation (ex. ligue jamais suivie par get_division_calendar,
        # timeout MPG). Le picker retombe sur "tout deverrouille" (quantite
        # pleine, non decrementee) plutot que de faire echouer toute la
        # recommandation.
        bonus_remaining = bonus_remaining_counts(len(teams), {})

    return jsonify({
        "formation": rec["formation"] if rec else None,
        "starters": [resolve(s) for s in rec["starters"]] if rec else [],
        "bench": [resolve(b) for b in rec["bench"]] if rec else [],
        "captainId": rec["captainId"] if rec else None,
        "squad": squad,
        "formations": FORMATIONS,
        "bonusRemaining": bonus_remaining,
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8766))
    app.run(host="0.0.0.0", port=port, debug=False)
