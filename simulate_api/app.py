"""
Fonction cloud (Flask, hebergement externe -- ex. Render) exposant
compute_division_live_scores avec bonus_choices HYPOTHETIQUES, pour que le
site statique mpg_live (GitHub Pages, aucune capacite de calcul) puisse
proposer aux managers "et si je joue tel coup / mon adversaire joue tel
autre" pendant une journee EN COURS -- retour utilisateur 2026-08-24 :
"a chaque fois qu'une journee commencera, on ajoutera les points
theoriques a l'instant T et on permettra aux managers de faire des
simulations de situations selon les bonus mis ou non, comme developpe
avec lancer_live.bat".

Port de UN SEUL endpoint de admin_server.py (mpg_app) pour ce premier
increment -- la simulation "Mon Bonus" par match (pas le simulateur de
journee acceleree GameweekSimulation, une fonctionnalite distincte, plus
lourde, pas demandee ici). Reutilise core/live_scoring.py/core/api.py TELS
QUELS (aucune reimplementation JS de la logique de scoring -- decision
utilisateur 2026-08-24, "petite fonction cloud Python").

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

from core.api import get_division_matches, get_championship_match
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


@app.route("/simulate-bonus", methods=["GET", "OPTIONS"])
def simulate_bonus():
    """Reproduit tel quel admin_server.py::api_simulate_my_bonus (mpg_app) --
    memes parametres, meme contrat. Params (query string) :
    shortId, season, division, gameweek, userId (obligatoires),
    ownBonus/targetPlayerId/opponentBonus (optionnels -- le coup HYPOTHETIQUE
    a tester, pas necessairement celui reellement declare sur MPG).
    Renvoie le match recalcule (home/away, "score", "total", badges...)
    exactement comme s'il avait ete joue avec ces choix -- ne modifie RIEN
    sur MPG ni Supabase, purement un calcul en memoire."""
    if request.method == "OPTIONS":
        return "", 204

    short_id = request.args.get("shortId")
    season = request.args.get("season")
    division = request.args.get("division")
    game_week = request.args.get("gameweek")
    user_id = request.args.get("userId")
    own_bonus = request.args.get("ownBonus") or None
    target_player_id = request.args.get("targetPlayerId") or None
    opponent_bonus = request.args.get("opponentBonus") or None

    if not all([short_id, season, division, game_week, user_id]):
        return jsonify({"error": "parametres manquants"}), 400
    for bonus in (own_bonus, opponent_bonus):
        if bonus and bonus not in VALID_BONUSES:
            return jsonify({"error": f"Bonus inconnu ou pas encore supporte en live : {bonus}"}), 400

    division_matches = get_division_matches(short_id, int(season), int(division), int(game_week))
    div_match = next(
        (m for m in division_matches if user_id in (m["home"].get("userId"), m["away"].get("userId"))), None,
    )
    if not div_match:
        return jsonify({"error": "Match introuvable pour ce manager dans cette division"}), 404
    opponent_user_id = (
        div_match["away"]["userId"] if div_match["home"].get("userId") == user_id else div_match["home"]["userId"]
    )

    division_size = len(division_matches) * 2
    for bonus in (own_bonus, opponent_bonus):
        if bonus and not bonus_available_for_division_size(bonus, division_size):
            return jsonify({"error": f"{bonus} n'existe pas pour une division de {division_size} (tableau officiel MPG)"}), 400

    bonus_choices = {}
    if own_bonus:
        bonus_choices[user_id] = {"bonus": own_bonus, "targetPlayerId": target_player_id}
    if opponent_bonus:
        bonus_choices[opponent_user_id] = {"bonus": opponent_bonus, "targetPlayerId": None}

    match_ids = collect_real_match_ids([div_match])
    real_matches_by_id = {mid: get_championship_match(mid) for mid in match_ids}
    match = compute_division_live_scores([div_match], real_matches_by_id, bonus_choices)[0]

    return jsonify(match)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8766))
    app.run(host="0.0.0.0", port=port, debug=False)
