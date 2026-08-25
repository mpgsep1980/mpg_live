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

from core.api import get_division_matches, get_championship_match, get_division_info
from core.live_scoring import (
    compute_division_live_scores, collect_real_match_ids, VALID_BONUSES, bonus_available_for_division_size,
)

app = Flask(__name__)


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
    outfield_players = [(pid, p["name"]) for pid, p in opponent_team["players"].items() if p["position"] != 1]

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


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8766))
    app.run(host="0.0.0.0", port=port, debug=False)
