"""
Mode admin : petite page HTML locale pour piloter les paramètres de calcul
(nombre de divisions, pondérations par division, bonus suivis) sans toucher
au code. Lit/écrit League_Codes.json via core/league.py.

Local uniquement (127.0.0.1) — ne pas exposer sur le réseau, League_Codes.json
n'a pas de contrôle d'accès.

Lancer : python admin_server.py — ouvre http://127.0.0.1:5055 automatiquement.
"""
import json
import secrets
import sys
import threading
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from flask import Flask, jsonify, request, send_from_directory, session

from config import BASE_PATH
from core.league import (
    get_all_leagues, update_league, get_scoring_config,
    get_number_of_divisions, get_internal_bonus_config, compute_scoring_override,
)
from core.scoring import BONUS_LABELS
from core.general_bonus import load_general_bonus_config, save_general_bonus_config, ALL_LEAGUE_NAMES
from core.multi_boss import load_multi_boss_config, save_multi_boss_config
from core.users import get_user, get_all_users
from core.live_projection import (
    LIVE_SNAPSHOT_DIR, LIVE_SNAPSHOT_NAME_RE, compute_provisional_super_classement,
    compute_provisional_division_classement, compute_provisional_league_classement,
    is_gameweek_archived, load_base_league_classement, POINTS_FORMULA_BONUS_KEYS, LIVE_COUNTER_KEYS,
)
from core.api import get_division_matches, get_championship_match, test_token
from core.token import load_token, save_token, token_is_set
from core.live_scoring import (
    compute_division_live_scores, collect_real_match_ids, VALID_BONUSES, bonus_available_for_division_size,
    detect_confirmed_bonus_choices,
)
from core.gameweek_simulator import GameweekSimulation
from core.auth import (
    ADMIN_USER_IDS, has_password, set_password, verify_password, clear_password, load_accounts,
)

app = Flask(__name__, static_folder=None)
ADMIN_DIR = Path(__file__).parent / "admin"
GENERAL_BONUS_CONFIG_PATH = BASE_PATH / "Classement_General" / "general_bonus_config.json"
MULTI_BOSS_CONFIG_PATH = BASE_PATH / "Classement_General" / "multi_boss_config.json"
BLASONS_DIR = BASE_PATH / "Joueurs" / "Blasons"

# Cle de session Flask persistee sur disque -- generee une seule fois (sinon
# chaque redemarrage du serveur deconnecterait tout le monde, gennant vu la
# frequence des redemarrages en developpement). Fichier local, pas de secret
# partage ailleurs.
_SECRET_KEY_PATH = Path(__file__).parent / "config" / "flask_secret.key"
if not _SECRET_KEY_PATH.exists():
    _SECRET_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    _SECRET_KEY_PATH.write_text(secrets.token_hex(32), encoding="utf-8")
app.secret_key = _SECRET_KEY_PATH.read_text(encoding="utf-8").strip()

LEAGUE_FIELDS = ("playersNumber", "poolGameweeks", "Div_A_Gameweeks", "seasonStart", "seasonSearch")

# Simulation de journee "acceleree" (core/gameweek_simulator.py) -- une seule a
# la fois (process Flask dev mono-processus), remplacee/arretee avant d'en
# relancer une nouvelle. Purement un outil de test, cf. docstring du module.
_active_simulation: GameweekSimulation | None = None


@app.get("/theme.css")
def theme_css():
    return send_from_directory(ADMIN_DIR, "theme.css")


@app.get("/mpg-tokens.css")
def mpg_tokens_css():
    return send_from_directory(ADMIN_DIR, "mpg-tokens.css")


@app.get("/")
def index():
    return send_from_directory(ADMIN_DIR, "home.html")


@app.get("/config")
def config_page():
    return send_from_directory(ADMIN_DIR, "admin.html")


@app.get("/token")
def token_page():
    return send_from_directory(ADMIN_DIR, "token.html")


@app.get("/api/token")
def api_token_status():
    """Etat actuel du token (masque -- jamais renvoye en clair) -- retour
    utilisateur 2026-08-17 : sur ce depot recentre "live uniquement", la page
    Streamlit qui permettait de le renseigner (pages/1_Token_API.py) est
    volontairement exclue (pipeline "Fetch & Generer") -- un collaborateur qui
    clone n'avait plus aucun moyen simple de savoir/renseigner son token.
    Reprend ici la meme logique (core/token.py, deja pret mais jamais branche
    a admin_server.py) pour rester utilisable sans l'app Streamlit."""
    current = load_token()
    masked = f"{current[:30]}...{current[-10:]}" if len(current) > 40 else current
    return jsonify({"isSet": token_is_set(), "masked": masked if current else None})


@app.post("/api/token")
def api_token_save():
    payload = request.get_json(force=True, silent=True) or {}
    new_token = (payload.get("token") or "").strip()
    if not new_token:
        return jsonify({"error": "Token vide"}), 400
    save_token(new_token)
    return jsonify({"saved": True})


@app.post("/api/token/test")
def api_token_test():
    """Teste soit le token DEJA enregistre (aucun 'token' dans le payload),
    soit un candidat pas encore sauvegarde (previsualisation avant
    d'enregistrer, meme UX que l'ancienne page Streamlit)."""
    payload = request.get_json(force=True, silent=True) or {}
    candidate = (payload.get("token") or "").strip() or None
    ok, message = test_token(candidate)
    return jsonify({"ok": ok, "message": message})


@app.get("/manager")
def manager_page():
    return send_from_directory(ADMIN_DIR, "manager.html")


@app.get("/live")
def live_page():
    return send_from_directory(ADMIN_DIR, "live_division.html")


@app.get("/simulator")
def simulator_page():
    return send_from_directory(ADMIN_DIR, "simulator.html")


@app.get("/super-classement")
def super_classement_page():
    return send_from_directory(ADMIN_DIR, "super_classement.html")


@app.get("/poules")
def poules_page():
    return send_from_directory(ADMIN_DIR, "poules.html")


@app.get("/classement-ligue")
def classement_ligue_page():
    return send_from_directory(ADMIN_DIR, "classement_ligue.html")


@app.get("/comptes")
def comptes_page():
    return send_from_directory(ADMIN_DIR, "comptes.html")


@app.get("/blasons/<path:filename>")
def blason_image(filename):
    """Sert Joueurs/Blasons/*.png (nomme "{RealName}_blason.png", cf.
    /api/managers) -- seuls 19 des ~80 managers en ont un, 404 attendu pour
    les autres, gere cote client (onerror)."""
    return send_from_directory(BLASONS_DIR, filename)


def _is_owner(user_id: str) -> bool:
    """True si la session en cours est authentifiee EN TANT QUE ce manager
    precis -- seule verification qui compte pour debloquer SES controles de
    bonus (cf. core/auth.py docstring). Le statut admin n'intervient JAMAIS
    ici, meme pour un admin (ADMIN_USER_IDS) sur le compte d'un autre -- retour
    utilisateur 2026-08-12 : "je ne veux pas d'acces aux bonus des autres"."""
    return bool(user_id) and session.get("userId") == user_id


def _is_admin() -> bool:
    return session.get("userId") in ADMIN_USER_IDS


@app.get("/api/auth/status")
def api_auth_status():
    """Etat de connexion pour un userId cible (page consultee) -- le manager
    lui-meme doit se reconnecter par page/onglet (pas de connexion "globale"
    au site), volontaire : evite qu'une session laissee ouverte sur le poste
    d'un manager donne acces aux bonus d'un AUTRE manager visite ensuite sur
    la meme machine."""
    target_user_id = request.args.get("userId", "")
    return jsonify({
        "loggedInUserId": session.get("userId"),
        "isOwner": _is_owner(target_user_id),
        "isAdmin": _is_admin(),
        "hasPassword": has_password(target_user_id) if target_user_id else False,
    })


@app.get("/api/auth/accounts-status")
def api_auth_accounts_status():
    """Liste des userId ayant deja un mot de passe -- reserve a l'admin, pour
    une vue d'ensemble/gestion des comptes (retour utilisateur 2026-08-13 :
    "en tant qu'Admin, je dois y avoir acces pour modifier au besoin"). Ne
    renvoie jamais les hash eux-memes, juste QUI en a un -- la creation/
    modification reutilise /api/auth/set-password (deja permissif pour
    l'admin, cf. sa docstring) et la reinitialisation
    /api/auth/admin-reset-password, tous deux existants."""
    if not _is_admin():
        return jsonify({"error": "Reserve a l'admin"}), 403
    return jsonify({"userIdsWithPassword": list(load_accounts().keys())})


@app.post("/api/auth/login")
def api_auth_login():
    payload = request.get_json(force=True) or {}
    user_id = payload.get("userId", "")
    password = payload.get("password", "")
    if not user_id or not password:
        return jsonify({"error": "userId et password requis"}), 400
    if not verify_password(user_id, password):
        return jsonify({"error": "Mot de passe incorrect"}), 401
    session["userId"] = user_id
    return jsonify({"ok": True})


@app.post("/api/auth/logout")
def api_auth_logout():
    session.pop("userId", None)
    return jsonify({"ok": True})


@app.post("/api/auth/set-password")
def api_auth_set_password():
    """Cree/modifie le mot de passe d'un manager -- en libre-service
    SEULEMENT si ce manager n'en a pas encore (premiere connexion), sinon
    reserve a l'admin (mdp perdu ou modification directe, retour utilisateur
    2026-08-13 : "en tant qu'Admin, je dois y avoir acces pour modifier au
    besoin")."""
    payload = request.get_json(force=True) or {}
    user_id = payload.get("userId", "")
    password = payload.get("password", "")
    if not user_id or not password:
        return jsonify({"error": "userId et password requis"}), 400
    if len(password) < 4:
        return jsonify({"error": "Mot de passe trop court (4 caracteres minimum)"}), 400
    if has_password(user_id) and not _is_admin():
        return jsonify({"error": "Un mot de passe existe deja -- demande a l'admin de le reinitialiser"}), 403
    set_password(user_id, password)
    # Auto-connexion SEULEMENT si personne n'est deja connecte sous une AUTRE
    # identite -- sinon l'admin qui definit le mdp d'un AUTRE manager se
    # retrouverait deconnecte de son propre compte et connecte a la place
    # sous celui du manager cible (bug trouve en concevant la gestion des
    # comptes cote admin).
    if session.get("userId") in (None, user_id):
        session["userId"] = user_id
    return jsonify({"ok": True})


@app.post("/api/auth/change-password")
def api_auth_change_password():
    """Change son PROPRE mot de passe alors qu'on est deja connecte -- couvre
    le cas "je suis encore connecte (session persistee) mais je ne me
    souviens plus de mon mot de passe" sans passer par l'admin (retour
    utilisateur 2026-08-13 : "laisser la possibilite aux joueurs de modifier
    leur mot de passe en cas d'oubli"). Ne redemande PAS l'ancien mot de
    passe -- la session active (_is_owner) fait deja foi d'identite, meme
    niveau de confiance que le reste des controles proteges par _is_owner
    (cf. core/auth.py). Si le manager n'a PLUS de session active du tout,
    seul l'admin peut reinitialiser (/api/auth/admin-reset-password)."""
    payload = request.get_json(force=True) or {}
    user_id = payload.get("userId", "")
    new_password = payload.get("newPassword", "")
    if not user_id or not new_password:
        return jsonify({"error": "userId et newPassword requis"}), 400
    if not _is_owner(user_id):
        return jsonify({"error": "Connecte-toi en tant que ce manager pour changer son mot de passe"}), 403
    if len(new_password) < 4:
        return jsonify({"error": "Mot de passe trop court (4 caracteres minimum)"}), 400
    set_password(user_id, new_password)
    return jsonify({"ok": True})


@app.post("/api/auth/admin-reset-password")
def api_auth_admin_reset_password():
    """Reinitialisation d'un mdp perdu -- reserve aux admins (ADMIN_USER_IDS), NE DONNE
    PAS a l'admin acces au compte cible (cf. core/auth.py::clear_password) :
    supprime juste le mot de passe existant, le manager en recree un a sa
    prochaine connexion via /api/auth/set-password."""
    if not _is_admin():
        return jsonify({"error": "Reserve a l'admin"}), 403
    payload = request.get_json(force=True) or {}
    user_id = payload.get("userId", "")
    if not user_id:
        return jsonify({"error": "userId requis"}), 400
    clear_password(user_id)
    return jsonify({"ok": True})


@app.get("/api/live-snapshot")
def api_live_snapshot():
    """Relit le fichier ecrit par live_watch.py (pas de recalcul complet ici --
    juste la derniere photo en date, deja produite par le poll en arriere-plan).
    Si cette journee est DEJA ARCHIVEE, reconstruit le VRAI resultat de chaque
    division via _reconstruct_archived_division (meme mecanisme que
    /api/live-manager) -- le snapshot peut sinon rester fige sur un etat
    intermediaire d'avant la fin reelle du match. Repli degrade si le refetch
    echoue : score+/score- de la base archivee -- MAIS CE CHAMP EST CUMULE SUR
    TOUTE LA SAISON SUIVIE (buts_pour total, pas ceux de CETTE journee seule),
    cf. core.live_projection._combine_row -- ne l'utiliser que comme dernier
    recours, jamais comme correction par defaut. Bug corrige 2026-08-16 :
    avant ce correctif, TOUTE journee archivee passait systematiquement par ce
    repli cumulatif (jamais la reconstruction), affichant par exemple 5 buts
    sur un seul match J1 pour un joueur qui n'en avait marque que 3 en J1 et 2
    en J2 (retour utilisateur, capture Ligue_2_EKT)."""
    short_id = request.args.get("shortId", "QLRBXDCX2")
    season = request.args.get("season", "21")
    game_week = request.args.get("gameweek", "1")
    path = LIVE_SNAPSHOT_DIR / f"{short_id}_{season}_gw{game_week}.json"
    if not path.exists():
        return jsonify({"error": f"Pas d'instantane pour {short_id}/{season}/gw{game_week} -- live_watch.py tourne-t-il ?"}), 404

    data = json.loads(path.read_text(encoding="utf-8"))
    league = next((l for l in get_all_leagues() if l["code"] == short_id), None)
    base_by_user = load_base_league_classement(league["nom"]) if league else {}

    archived = False
    if base_by_user:
        sample_user_id = next(
            (t.get("userId")
             for divs in data.get("divisions", {}).values() for m in divs
             for t in (m["home"], m["away"]) if t.get("userId")),
            None,
        )
        if sample_user_id:
            archived = is_gameweek_archived(league["nom"], [sample_user_id], int(game_week))

    for division_str, division_matches in data.get("divisions", {}).items():
        parsed_scores_by_id = (
            _load_parsed_division_scores(league["nom"], int(season), int(division_str), game_week)
            if archived and league else None
        )
        # Refetch reseau seulement si le pipeline n'a pas encore ete
        # re-execute pour cette journee (parsed_scores_by_id vide) -- couteux,
        # dernier recours avant le repli cumulatif.
        reconstructed_by_id = (
            _reconstruct_archived_division(short_id, int(season), int(division_str), int(game_week))
            if archived and not parsed_scores_by_id else None
        )
        fallback_needed = archived and not parsed_scores_by_id and reconstructed_by_id is None

        for idx, match in enumerate(division_matches):
            if reconstructed_by_id:
                fresh = reconstructed_by_id.get(match.get("matchId"))
                if fresh:
                    division_matches[idx] = match = fresh
            parsed_match = parsed_scores_by_id.get(match.get("matchId")) if parsed_scores_by_id else None
            for side in ("home", "away"):
                team = match[side]
                user = get_user(team.get("userId"))
                team["realName"] = user.get("RealName") if user else None
                team["teamName"] = user.get(short_id) if user else None
                if parsed_match and parsed_match.get(side, {}).get("score") is not None:
                    team["score"] = parsed_match[side]["score"]
                elif fallback_needed:
                    base_row = base_by_user.get(team.get("userId"))
                    if base_row:
                        team["score"] = base_row.get("score+", team.get("score"))
    data["archived"] = archived
    return jsonify(data)


@app.get("/api/division-classement")
def api_division_classement():
    """Classement provisoire d'UNE division (base archivee + delta live, cf.
    core/live_projection.py::compute_provisional_division_classement) --
    affiche a cote des matchs en cours (admin/live_division.html et
    admin/manager.html), pour voir la position provisoire de tout le monde
    dans la division, pas seulement le total de points d'un manager isole.
    Si userId/ownBonus (+ targetPlayerId) sont fournis, le classement reflete
    AUSSI cette simulation "Mon Bonus" pour le match de ce manager -- meme
    principe et meme fetch frais que /api/manager-classement (retour
    utilisateur : le plateau et le score affichaient deja la simulation, mais
    ce classement de division restait sur le reel -- incoherent)."""
    short_id = request.args.get("shortId", "QLRBXDCX2")
    season = request.args.get("season", "21")
    game_week = request.args.get("gameweek", "1")
    division = request.args.get("division")
    if not division:
        return jsonify({"error": "division manquante"}), 400

    league = next((l for l in get_all_leagues() if l["code"] == short_id), None)
    if not league:
        return jsonify({"error": f"Ligue introuvable pour shortId={short_id}"}), 404

    path = LIVE_SNAPSHOT_DIR / f"{short_id}_{season}_gw{game_week}.json"
    if not path.exists():
        return jsonify({"error": f"Pas d'instantane pour {short_id}/{season}/gw{game_week} -- live_watch.py tourne-t-il ?"}), 404
    live_snapshot = json.loads(path.read_text(encoding="utf-8"))

    user_id = request.args.get("userId")
    own_bonus = request.args.get("ownBonus") or None
    target_player_id = request.args.get("targetPlayerId") or None

    if user_id and own_bonus:
        if not _is_owner(user_id):
            return jsonify({"error": "Connecte-toi en tant que ce manager pour simuler ses bonus"}), 403
        if own_bonus not in VALID_BONUSES:
            return jsonify({"error": f"Bonus inconnu ou pas encore supporte en live : {own_bonus}"}), 400
        sim_division_matches = get_division_matches(short_id, int(season), int(division), int(game_week))
        sim_division_size = len(sim_division_matches) * 2
        if not bonus_available_for_division_size(own_bonus, sim_division_size):
            return jsonify({"error": f"{own_bonus} n'existe pas pour une division de {sim_division_size} (tableau officiel MPG)"}), 400

        sim_match_ids = collect_real_match_ids(sim_division_matches)
        sim_real_matches = {mid: get_championship_match(mid) for mid in sim_match_ids}
        sim_results = compute_division_live_scores(
            sim_division_matches, sim_real_matches, {user_id: {"bonus": own_bonus, "targetPlayerId": target_player_id}},
        )
        live_snapshot = dict(live_snapshot)
        live_snapshot["divisions"] = dict(live_snapshot.get("divisions", {}))
        live_snapshot["divisions"][str(int(division))] = sim_results

    rows = compute_provisional_division_classement(league, live_snapshot, int(game_week), int(division))
    return jsonify({"rows": rows, "simulated": bool(user_id and own_bonus)})


@app.get("/api/league-classement")
def api_league_classement():
    """Classement provisoire d'UNE ligue ENTIERE (toutes divisions confondues,
    trie sur "points" -- le total officiel pondere+bonus, meme tri que
    Ligue_2_EKT_League_Place dans la fusion Super Classement) -- pendant de
    /api/division-classement mais a l'echelle de la ligue plutot que d'une
    seule division. Ajoute avec le simulateur de journee (retour utilisateur
    2026-08-12) pour voir "le classement de Ligue 2" bouger, pas juste une
    poule."""
    short_id = request.args.get("shortId", "QLRBXDCX2")
    season = request.args.get("season", "21")
    game_week = request.args.get("gameweek", "1")

    league = next((l for l in get_all_leagues() if l["code"] == short_id), None)
    if not league:
        return jsonify({"error": f"Ligue introuvable pour shortId={short_id}"}), 404

    path = LIVE_SNAPSHOT_DIR / f"{short_id}_{season}_gw{game_week}.json"
    if not path.exists():
        return jsonify({"error": f"Pas d'instantane pour {short_id}/{season}/gw{game_week} -- live_watch.py tourne-t-il ?"}), 404
    live_snapshot = json.loads(path.read_text(encoding="utf-8"))

    rows = compute_provisional_league_classement(league, live_snapshot, int(game_week))
    ranked = sorted(
        rows,
        key=lambda x: (-x.get("points", 0), -x.get("score_diff", 0), -x.get("score+", 0), x.get("score-", 0)),
    )
    for i, row in enumerate(ranked, start=1):
        row["rang"] = i
    return jsonify({"rows": ranked})


@app.get("/api/super-classement")
def api_super_classement():
    """Super Classement provisoire complet (toutes ligues, cf.
    compute_provisional_super_classement) -- pendant de /api/manager-classement
    mais pour TOUT LE MONDE d'un coup plutot qu'un seul manager, pour le
    simulateur de journee (retour utilisateur 2026-08-12)."""
    ranked = compute_provisional_super_classement()
    return jsonify({"rows": ranked})


@app.get("/api/simulate-gameweeks")
def api_simulate_gameweeks():
    """Journees (gameweek) actuellement presentes sur disque pour cette
    ligue/saison (live_snapshots/{shortId}_{season}_gw{N}.json, qu'elles
    soient reelles-archivees comme J1 ou simulees comme J2/J3) -- sert au
    simulateur a proposer un enchainement par defaut (derniere journee
    presente +1) plutot que de rester fige sur une journee fixe (retour
    utilisateur 2026-08-12 : "enchainer une simulation de la J3 au lieu de
    recommencer la J2")."""
    short_id = request.args.get("shortId", "QLRBXDCX2")
    season = request.args.get("season", "21")
    gameweeks = []
    for path in LIVE_SNAPSHOT_DIR.glob(f"{short_id}_{season}_gw*.json"):
        m = LIVE_SNAPSHOT_NAME_RE.match(path.name)
        if m:
            gameweeks.append(int(m.group(3)))
    return jsonify({"gameweeks": sorted(gameweeks)})


@app.post("/api/simulate-start")
def api_simulate_start():
    """Lance une simulation de journee "acceleree" (core/gameweek_simulator.py)
    -- reutilise les compositions/notes d'une journee DEJA jouee (sourceGameweek,
    typiquement la derniere archivee) comme gabarit, remet les scores a zero et
    place des buts aleatoires dans TOUTES les divisions, revele minute par
    minute sur `durationSeconds` (defaut 90). Ecrit progressivement dans
    live_snapshots/{shortId}_{season}_gw{gameweek}.json -- consomme ensuite par
    tous les endpoints/pages existants sans code specifique. Purement un outil
    de test (retour utilisateur 2026-08-12), n'ecrit jamais dans un fichier
    archive/officiel."""
    if not _is_admin():
        return jsonify({"error": "Reserve a l'admin"}), 403
    global _active_simulation
    payload = request.get_json(force=True) or {}
    short_id = payload.get("shortId", "QLRBXDCX2")
    season = payload.get("season", "21")
    game_week = payload.get("gameweek", 2)
    source_game_week = payload.get("sourceGameweek", 1)
    duration_seconds = payload.get("durationSeconds", 90)

    source_path = LIVE_SNAPSHOT_DIR / f"{short_id}_{season}_gw{source_game_week}.json"
    if not source_path.exists():
        return jsonify({"error": f"Pas d'instantane source pour {short_id}/{season}/gw{source_game_week} a utiliser comme gabarit"}), 404
    source_snapshot = json.loads(source_path.read_text(encoding="utf-8"))

    if _active_simulation and _active_simulation.running:
        _active_simulation.stop()

    target_path = LIVE_SNAPSHOT_DIR / f"{short_id}_{season}_gw{game_week}.json"
    _active_simulation = GameweekSimulation(
        target_path, short_id, season, source_game_week, game_week, duration_seconds=duration_seconds,
    )
    _active_simulation.start(source_snapshot)
    return jsonify({"status": "started", **_active_simulation.status()})


@app.get("/api/simulate-status")
def api_simulate_status():
    if not _active_simulation:
        return jsonify({"running": False, "minute": 0, "totalMinutes": 90})
    return jsonify(_active_simulation.status())


@app.post("/api/simulate-stop")
def api_simulate_stop():
    if not _is_admin():
        return jsonify({"error": "Reserve a l'admin"}), 403
    if _active_simulation:
        _active_simulation.stop()
    return jsonify({"status": "stopped"})


@app.post("/api/simulate-delete")
def api_simulate_delete():
    """Supprime le fichier live_snapshots/{shortId}_{season}_gw{gameweek}.json
    d'une simulation (arrete d'abord la simulation en cours si elle vise CE
    meme fichier) -- retour utilisateur 2026-08-12 : repartir d'une ardoise
    propre plutot que garder un etat de test fige (ex. simulation interrompue
    en plein match, cf. incident "0-5" qui a suivi la simulation arretee au
    mauvais moment). Reserve a l'admin, comme le reste du simulateur."""
    global _active_simulation
    if not _is_admin():
        return jsonify({"error": "Reserve a l'admin"}), 403
    payload = request.get_json(force=True) or {}
    short_id = payload.get("shortId", "QLRBXDCX2")
    season = payload.get("season", "21")
    game_week = payload.get("gameweek", 2)

    target_path = LIVE_SNAPSHOT_DIR / f"{short_id}_{season}_gw{game_week}.json"
    if _active_simulation and _active_simulation.snapshot_path == target_path:
        _active_simulation.stop()
        _active_simulation = None
    deleted = target_path.exists()
    target_path.unlink(missing_ok=True)
    return jsonify({"ok": True, "deleted": deleted})


@app.get("/api/managers")
def api_managers():
    """Liste officielle des managers (userId global + RealName) -- "officiel" =
    RealName present ET Actif != False dans MPG_Users.json (cf. retour
    utilisateur 2026-08-10 : 72 managers exactement, exclut les entrees
    perimees/historiques du fichier)."""
    managers = [
        {
            "userId": u["userId"], "realName": u.get("RealName"), "avatarUrl": u.get("avatarUrl"),
            "markerColour": u.get("MarkerColour"),
            "blasonUrl": f"/blasons/{u['RealName']}_blason.png",
        }
        for u in get_all_users() if u.get("RealName") and u.get("Actif", True) is not False
    ]
    managers.sort(key=lambda m: m["realName"].lower())
    return jsonify({"managers": managers})


def _parsed_scores_path(league_name: str, season: int, division: int, game_week) -> Path:
    return (
        BASE_PATH / league_name / f"saison_{season:03d}" / "Parsed" / f"Division_{division:02d}" / "Scores"
        / f"{league_name}_saison_{season:03d}_Division_{division:02d}_Journee_{game_week}_Scores.json"
    )


def _load_parsed_division_scores(league_name: str, season: int, division: int, game_week) -> dict[str, dict] | None:
    """Lit le resultat PAR MATCH (pas cumule) deja calcule par le pipeline
    notebook existant (Parsed/Division_XX/Scores/*_Scores.json, ecrit par
    "Fetch & Générer" -- cf. memoire projet, aucun recalcul/refetch necessaire
    ici). Retour utilisateur 2026-08-16 : plutot qu'un cache reseau, lire ce
    stockage qui existe deja -- {MatchId: {"home": {"score", "realName",
    "userId"}, "away": {...}}}, None si le fichier n'existe pas encore (journee
    tout juste archivee, pipeline pas encore re-execute pour elle -- appelant
    a un repli)."""
    path = _parsed_scores_path(league_name, season, division, game_week)
    if not path.exists():
        return None
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    by_match: dict[str, dict] = {}
    for entry in entries:
        match_id = entry.get("MatchId")
        if not match_id:
            continue
        by_match[match_id] = {
            side: {
                "score": entry[side].get("Score+"),
                "realName": entry[side].get("RealName"),
                "userId": entry[side].get("userId"),
            }
            for side in ("home", "away") if side in entry
        }
    return by_match


def _reconstruct_archived_division(short_id: str, season: int, division: int, game_week: int) -> dict[str, dict] | None:
    """Reconstruction complete de TOUS les matchs d'une division DEJA ARCHIVEE
    (cf. is_gameweek_archived cote appelant) : refetch FRAIS de
    get_division_matches (MPG revele desormais player["bonusesDetails"],
    invisible pendant le direct -- meme principe que la resolution des
    remplacements tactiques post-match), detection des coups reellement
    confirmes (core.live_scoring.detect_confirmed_bonus_choices) et recalcul
    via compute_division_live_scores -- reproduit le VRAI resultat final
    (score, buteurs, notes boostees/nerfees) au lieu du score/buteurs figes du
    snapshot live (capture avant la fin reelle du match, coups jamais visibles
    en direct). Retour utilisateur 2026-08-11 : "Sainte-Luce a 6 [...] il a
    été boosté par un McDo et devrait avoir 7" -- verifie exact contre
    /division-match/... une fois le match termine. {matchId: match}, None si
    le refetch/la recomputation echoue (reseau, division introuvable...) --
    appelant a un repli degrade.

    Utilise UNIQUEMENT si _load_parsed_division_scores n'a rien trouve
    (journee tout juste archivee, pipeline pas encore re-execute) -- couteux
    (fetch reseau complet de la division), a eviter en usage normal."""
    try:
        division_matches = get_division_matches(short_id, season, division, game_week)
        bonus_choices = detect_confirmed_bonus_choices(division_matches)
        match_ids = collect_real_match_ids(division_matches)
        real_matches_by_id = {mid: get_championship_match(mid) for mid in match_ids}
        results = compute_division_live_scores(division_matches, real_matches_by_id, bonus_choices)
        return {m["matchId"]: m for m in results}
    except Exception:
        return None


def _reconstruct_archived_match(short_id: str, season: int, division: int, game_week: int, user_id: str) -> dict | None:
    """Meme reconstruction que _reconstruct_archived_division, filtree au
    match d'UN manager -- cf. sa docstring pour le detail (utilise par
    /api/live-manager, qui n'a besoin que d'un seul match a la fois)."""
    by_id = _reconstruct_archived_division(short_id, season, division, game_week)
    if not by_id:
        return None
    return next(
        (m for m in by_id.values() if user_id in (m["home"].get("userId"), m["away"].get("userId"))), None,
    )


@app.get("/api/live-manager")
def api_live_manager():
    """Vue transversale d'un manager (userId global, cf. core/users.py) : union
    de ses matchs en cours dans TOUS les instantanes live_snapshots/ presents
    sur disque (un fichier par ligue/saison/journee -- alimentes par autant de
    live_watch.py qu'il y a de ligues suivies en parallele). team["userId"]
    dans un instantane est l'id de compte global (identique entre ligues), pas
    le teamId par ligue (id_{shortId} dans MPG_Users.json) -- pas besoin de le
    resoudre ici."""
    user_id = request.args.get("userId")
    if not user_id:
        return jsonify({"error": "userId manquant"}), 400

    leagues_by_code = {l["code"]: l for l in get_all_leagues()}
    matches = []
    for path in sorted(LIVE_SNAPSHOT_DIR.glob("*.json")):
        name_match = LIVE_SNAPSHOT_NAME_RE.match(path.name)
        if not name_match:
            continue
        short_id, season, game_week = name_match.groups()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        league = leagues_by_code.get(short_id)
        # Une journee DEJA archivee (match reel termine, resultat integre au
        # classement officiel) n'est plus "en cours" au sens simulation/bonus
        # -- mais son score et ses compositions restent consultables (retour
        # utilisateur 2026-08-11 : "ça n'empêche pas l'affichage du
        # classement de la division ainsi que l'accès aux données du match
        # précédent"). On la marque juste "archived" -- au client de retirer
        # les controles de simulation (plus aucun sens sur un match deja
        # tranche), pas les donnees elles-memes.
        archived = bool(league) and is_gameweek_archived(league["nom"], [user_id], int(game_week))

        for division, division_matches in data.get("divisions", {}).items():
            stale_match = next(
                (m for m in division_matches if user_id in (m["home"].get("userId"), m["away"].get("userId"))), None,
            )
            if not stale_match:
                continue

            match = stale_match
            score_detail_unreliable = False
            if archived and league:
                # Priorite au stockage LOCAL deja produit par le pipeline
                # (Parsed/Division_XX/Scores/*_Scores.json, score PAR MATCH --
                # cf. _load_parsed_division_scores) : instantane, pas de fetch
                # reseau. Retour utilisateur 2026-08-16 : cette vue (modale
                # manager, jusqu'a 6 matchs archives en meme temps) faisait un
                # refetch+recalcul reseau COMPLET par match (_reconstruct_archived_match),
                # ~10s pour 3 matchs -- le meme angle mort de lenteur que
                # /api/live-snapshot avant son propre correctif.
                parsed_scores = _load_parsed_division_scores(league["nom"], int(season), int(division), game_week)
                parsed_match = parsed_scores.get(match.get("matchId")) if parsed_scores else None
                if parsed_match:
                    for side in ("home", "away"):
                        if parsed_match.get(side, {}).get("score") is not None:
                            match[side]["score"] = parsed_match[side]["score"]
                else:
                    # Rien en local (journee tout juste archivee, pipeline pas
                    # encore re-execute) -- refetch+recalcul reseau complet,
                    # couteux mais precis (buteurs/notes inclus, pas juste le
                    # score).
                    reconstructed = _reconstruct_archived_match(short_id, int(season), int(division), int(game_week), user_id)
                    if reconstructed:
                        match = reconstructed
                    else:
                        # Dernier repli si le refetch/la recomputation echoue
                        # (reseau, division introuvable...) : au moins corriger
                        # le score affiche via l'archive officielle
                        # (score+/score-, CUMULE toute la saison -- signaler
                        # que le detail buteurs/notes du snapshot fige peut ne
                        # plus correspondre).
                        base_row = load_base_league_classement(league["nom"]).get(user_id)
                        if base_row:
                            for side, opp_side in (("home", "away"), ("away", "home")):
                                t = match[side]
                                if t.get("userId") != user_id:
                                    continue
                                o = match[opp_side]
                                official_own, official_opp = base_row.get("score+"), base_row.get("score-")
                                if t.get("score") != official_own or o.get("score") != official_opp:
                                    score_detail_unreliable = True
                                t["score"] = official_own if official_own is not None else t.get("score")
                                o["score"] = official_opp if official_opp is not None else o.get("score")
                                break

            for side, opp_side in (("home", "away"), ("away", "home")):
                team = match[side]
                if team.get("userId") != user_id:
                    continue
                opponent = match[opp_side]
                for t in (team, opponent):
                    user = get_user(t.get("userId"))
                    t["realName"] = user.get("RealName") if user else None
                    t["teamName"] = user.get(short_id) if user else None
                matches.append({
                    "shortId": short_id,
                    "ligueNom": league["nom"] if league else short_id,
                    "season": season, "gameweek": game_week, "division": division,
                    "updatedAt": data.get("updatedAt"),
                    "team": team, "opponent": opponent,
                    "archived": archived,
                    "scoreDetailUnreliable": score_detail_unreliable,
                    # Vraie position MPG (pas "team = toujours a gauche") --
                    # cf. retour utilisateur 2026-08-10 : afficher une fausse
                    # position domicile fausse le sens du tie-break du duel
                    # de lignes pour qui regarde. side vient de la boucle
                    # ci-dessus (side="home" -> team EST l'equipe domicile).
                    "isHome": side == "home",
                    # Nombre de participants de CETTE division -- determine
                    # quels bonus existent (tableau officiel MPG, cf.
                    # core.live_scoring.BONUS_MIN_DIVISION_SIZE, retour
                    # utilisateur 2026-08-11).
                    "divisionSize": len(division_matches) * 2,
                })
    return jsonify({"matches": matches})


@app.get("/api/manager-classement")
def api_manager_classement():
    """Position d'un manager au Super Classement provisoire -- base archivee +
    delta live (cf. core/live_projection.py), recalcule a chaque appel (pas de
    cache, les fichiers sources sont petits). Si shortId/season/division/
    gameweek/ownBonus sont fournis (memes params que "Mon Bonus" sur le
    plateau), le classement reflete AUSSI cette simulation pour CE match --
    fetch frais + recompute juste pour cette division, injecte a la place du
    snapshot disque le temps de l'appel (retour utilisateur 2026-08-11 : "je
    passe d'un match nul a une victoire, le classement ne bouge pas")."""
    user_id = request.args.get("userId")
    if not user_id:
        return jsonify({"error": "userId manquant"}), 400
    user = get_user(user_id)
    real_name = (user or {}).get("RealName")
    if not real_name:
        return jsonify({"found": False, "reason": "Manager introuvable dans MPG_Users.json"})

    own_bonus = request.args.get("ownBonus") or None
    target_player_id = request.args.get("targetPlayerId") or None
    sim_short_id = request.args.get("shortId")
    sim_season = request.args.get("season")
    sim_division = request.args.get("division")
    sim_gameweek = request.args.get("gameweek")

    snapshot_overrides = None
    if own_bonus and all([sim_short_id, sim_season, sim_division, sim_gameweek]):
        if not _is_owner(user_id):
            return jsonify({"error": "Connecte-toi en tant que ce manager pour simuler ses bonus"}), 403
        if own_bonus not in VALID_BONUSES:
            return jsonify({"error": f"Bonus inconnu ou pas encore supporte en live : {own_bonus}"}), 400
        sim_division_matches = get_division_matches(sim_short_id, int(sim_season), int(sim_division), int(sim_gameweek))
        sim_division_size = len(sim_division_matches) * 2
        if not bonus_available_for_division_size(own_bonus, sim_division_size):
            return jsonify({"error": f"{own_bonus} n'existe pas pour une division de {sim_division_size} (tableau officiel MPG)"}), 400

        sim_match_ids = collect_real_match_ids(sim_division_matches)
        sim_real_matches = {mid: get_championship_match(mid) for mid in sim_match_ids}
        sim_results = compute_division_live_scores(
            sim_division_matches, sim_real_matches, {user_id: {"bonus": own_bonus, "targetPlayerId": target_player_id}},
        )

        snapshot_path = LIVE_SNAPSHOT_DIR / f"{sim_short_id}_{sim_season}_gw{sim_gameweek}.json"
        base_snapshot = json.loads(snapshot_path.read_text(encoding="utf-8")) if snapshot_path.exists() else {"divisions": {}}
        base_snapshot = dict(base_snapshot)
        base_snapshot["divisions"] = dict(base_snapshot.get("divisions", {}))
        base_snapshot["divisions"][str(int(sim_division))] = sim_results
        snapshot_overrides = {sim_short_id: (base_snapshot, int(sim_gameweek))}

    ranked = compute_provisional_super_classement(snapshot_overrides=snapshot_overrides)
    stats = next((s for s in ranked if s.get("player_name") == real_name), None)
    if stats is None:
        return jsonify({"found": False, "reason": f"{real_name} absent du classement provisoire"})

    leagues = []
    for key, value in stats.items():
        if key.endswith("_League_Place"):
            league = key[: -len("_League_Place")]
            leagues.append({"league": league, "place": value, "points": stats.get(f"{league}_recap_points")})
    leagues.sort(key=lambda l: (l["place"] is None, l["place"]))

    # POINTS_FORMULA_BONUS_KEYS = EXACTEMENT ce qui compte dans "points" cote
    # serveur (cf. core/live_projection.py::_finalize_league_row) -- avant ce
    # correctif, cette liste etait dupliquee ici a la main, dupliquee EN
    # DIVERGENCE : Boss_Saison_* y etait inclus a tort (n'a jamais compte dans
    # "points", sert seulement au Multi/Triple Boss inter-ligues) et
    # "Precious" en etait absent (compte bien dans "points", cf. base
    # archivee). Resultat : le sous-total affiche cote client etait faux de
    # +1 pile (Precious manquant) -1 (Boss_Saison en trop) = ecart net
    # variable selon les managers -- retour utilisateur 2026-08-13 :
    # "qu'est-ce qui explique les 9.2 points... alors qu'on n'y ajoute que
    # 10 + 10 [de bonus generaux] ?" (attendu 8.2, pas 9.2). Reutilise
    # maintenant LA MEME constante que le calcul serveur, plus de liste
    # dupliquee possible a faire diverger.
    internal_bonuses = {k: stats[k] for k in POINTS_FORMULA_BONUS_KEYS if stats.get(k)}
    # Compteurs de match qui comptent AUSSI dans "points" cote officiel (cf.
    # update_team_points, cellules 17-18 de MPG_Ligue_2_EKT_Test_2025.ipynb :
    # points_pond + Pichichi + Le_Mur + cleanSheet + manita + Bonus_Podium +
    # Bonus_Champion + on_fire + Precious - grotaldo) mais qui n'etaient
    # JAMAIS renvoyes ici -- retour utilisateur 2026-08-16 : "math isn't
    # mathing", le sous-total affiche (points de ligue + bonus internes +
    # bonus generaux) ne pouvait pas retomber sur le total officiel puisque
    # ces 3-4 termes en etaient absents. grotaldo est un MALUS (deja soustrait
    # dans "points") -- expose negatif ici pour que son signe soit explicite
    # cote affichage plutot que de le laisser en implicite.
    match_counters = {k: stats[k] for k in LIVE_COUNTER_KEYS if stats.get(k)}
    if stats.get("grotaldo"):
        match_counters["Grotaldo"] = -stats["grotaldo"]
    # Boss_Saison_* : informatif seulement (lu par le Multi/Triple Boss
    # inter-ligues), ne compte PAS dans "points" -- separe pour ne pas etre
    # confondu avec ceux qui comptent. Bonus_Second/Bonus_Dernier
    # (compteurs 1/saison qui alimentent Poulidor/La Chevre cote Super
    # Classement, cf. core/general_bonus.py) exclus depuis le 2026-08-16 --
    # retour utilisateur : un "+1" affiche a cote de ces libelles laissait
    # penser a un joueur qu'un point comptabilise avait ete oublie du total,
    # alors que ce ne sont que des compteurs bruts sans valeur en points ici.
    other_bonuses = {}
    for key, value in stats.items():
        if "_Boss_Saison_" in key and value:
            other_bonuses[key] = value

    return jsonify({
        "found": True,
        "provisional": True,
        "simulated": snapshot_overrides is not None,
        "rank": stats.get("rang"),
        "totalEntries": len(ranked),
        "totalPoints": stats.get("points"),
        "leagues": leagues,
        "internalBonuses": internal_bonuses,
        "matchCounters": match_counters,
        "otherBonuses": other_bonuses,
        "generalBonusDetails": stats.get("bonus_details", {}),
        # Texte destine aux JOUEURS (retour utilisateur 2026-08-16 : le
        # precedent mentionnait live_watch.py/core/live_projection.py --
        # inutile et illisible pour un manager). Rester factuel sur ce que
        # "provisoire" recouvre, sans jargon d'implementation.
        "caveat": "Classement provisoire : inclut toutes les journées déjà jouées et celles en cours, "
                  "en tenant compte des remplacements tactiques.",
    })


@app.get("/api/live-scenario")
def api_live_scenario():
    """Recalcule EN DIRECT (fetch frais, pas le snapshot deja ecrit par
    live_watch.py) le match d'un manager avec des coups HYPOTHETIQUES -- pour
    explorer "et si je jouais X / mon adversaire jouait Y ?" sans RIEN
    persister (aucun store partage -- cf. docstring "Coups" dans
    core/live_scoring.py : un coup non confirme ne doit jamais influencer le
    match reel ni etre visible par l'autre manager). Renvoie match["home"]/
    ["away"] tels quels (vraie position MPG) -- ne PAS reordonner cote appelant,
    la position domicile/exterieur reelle conditionne le tie-break du duel de
    lignes (cf. retour utilisateur 2026-08-10)."""
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
    if not _is_owner(user_id):
        return jsonify({"error": "Connecte-toi en tant que ce manager pour simuler ses bonus"}), 403
    for bonus in (own_bonus, opponent_bonus):
        if bonus and bonus not in VALID_BONUSES:
            return jsonify({"error": f"Bonus inconnu ou pas encore supporte en live : {bonus}"}), 400

    division_matches = get_division_matches(short_id, int(season), int(division), int(game_week))
    div_match = next(
        (m for m in division_matches if user_id in (m["home"].get("userId"), m["away"].get("userId"))), None,
    )
    if not div_match:
        return jsonify({"error": "Match introuvable pour ce manager dans cette division"}), 404
    opponent_user_id = div_match["away"]["userId"] if div_match["home"].get("userId") == user_id else div_match["home"]["userId"]

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

    for team in (match["home"], match["away"]):
        user = get_user(team.get("userId"))
        team["realName"] = user.get("RealName") if user else None
        team["teamName"] = user.get(short_id) if user else None

    return jsonify(match)


@app.get("/api/live-scenario-sweep")
def api_live_scenario_sweep():
    """Etat des lieux complet des coups adverses possibles -- Zahia/Suarez/
    Cheat Code, plus McDo+ teste sur CHAQUE joueur de champ adverse (jamais le
    gardien, cf. reglement) -- en UN SEUL fetch reseau (division_matches +
    matchs reels recuperes une fois, puis chaque scenario est juste une
    recomputation en memoire, cf. retour utilisateur 2026-08-10 : "un etat des
    lieux qui gererait tous les cas de figure"). ownBonus/targetPlayerId (le
    coup du manager, optionnel) reste fixe sur tous les scenarios balayes."""
    short_id = request.args.get("shortId")
    season = request.args.get("season")
    division = request.args.get("division")
    game_week = request.args.get("gameweek")
    user_id = request.args.get("userId")
    own_bonus = request.args.get("ownBonus") or None
    own_target_player_id = request.args.get("targetPlayerId") or None

    if not all([short_id, season, division, game_week, user_id]):
        return jsonify({"error": "parametres manquants"}), 400
    if not _is_owner(user_id):
        return jsonify({"error": "Connecte-toi en tant que ce manager pour voir l'etat des lieux de ses bonus"}), 403
    if own_bonus and own_bonus not in VALID_BONUSES:
        return jsonify({"error": f"Bonus inconnu ou pas encore supporte en live : {own_bonus}"}), 400

    division_matches = get_division_matches(short_id, int(season), int(division), int(game_week))
    div_match = next(
        (m for m in division_matches if user_id in (m["home"].get("userId"), m["away"].get("userId"))), None,
    )
    if not div_match:
        return jsonify({"error": "Match introuvable pour ce manager dans cette division"}), 404
    opponent_user_id = div_match["away"]["userId"] if div_match["home"].get("userId") == user_id else div_match["home"]["userId"]

    division_size = len(division_matches) * 2
    if own_bonus and not bonus_available_for_division_size(own_bonus, division_size):
        return jsonify({"error": f"{own_bonus} n'existe pas pour une division de {division_size} (tableau officiel MPG)"}), 400

    match_ids = collect_real_match_ids([div_match])
    real_matches_by_id = {mid: get_championship_match(mid) for mid in match_ids}

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
        # invisible/pas verifiable sans regarder le plateau (retour
        # utilisateur 2026-08-10).
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

    # Seuls les bonus qui EXISTENT pour cette taille de division sont balayes
    # (tableau officiel MPG, cf. core.live_scoring.BONUS_MIN_DIVISION_SIZE,
    # retour utilisateur 2026-08-11) -- inutile/trompeur de tester un Miroir
    # dans une division de 4 ou il n'est pas encore alloue.
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


@app.get("/api/leagues")
def api_leagues():
    leagues = []
    for league in get_all_leagues():
        scoring = get_scoring_config(league)
        scoring["internalBonuses"] = get_internal_bonus_config(league)
        leagues.append({
            "code": league["code"],
            "nom": league["nom"],
            "playersNumber": league.get("playersNumber"),
            "poolGameweeks": league.get("poolGameweeks"),
            "Div_A_Gameweeks": league.get("Div_A_Gameweeks"),
            "seasonStart": league.get("seasonStart"),
            "seasonSearch": league.get("seasonSearch"),
            "numberOfDivisions": get_number_of_divisions(league),
            "scoring": scoring,
        })
    return jsonify({"leagues": leagues, "bonusCatalog": BONUS_LABELS})


@app.put("/api/leagues/<code>")
def api_update_league(code):
    payload = request.get_json(force=True) or {}
    updates = {key: payload[key] for key in LEAGUE_FIELDS if key in payload}
    if "scoring" in payload:
        updates["scoring"] = compute_scoring_override(payload["scoring"])
    if not updates:
        return jsonify({"error": "Aucun champ à mettre à jour"}), 400
    update_league(code, updates)
    return jsonify({"ok": True})


@app.put("/api/players-number")
def api_update_players_number():
    """Nombre de joueurs par ligue — commun aux 6 championnats, applique en une fois."""
    payload = request.get_json(force=True) or {}
    players_number = payload.get("playersNumber")
    if not isinstance(players_number, int) or players_number <= 0:
        return jsonify({"error": "playersNumber invalide"}), 400
    for league in get_all_leagues():
        update_league(league["code"], {"playersNumber": players_number})
    return jsonify({"ok": True})


@app.get("/api/general-bonuses")
def api_general_bonuses():
    config = load_general_bonus_config(GENERAL_BONUS_CONFIG_PATH)
    return jsonify({
        "categories": config["categories"],
        "includedLeagues": config["includedLeagues"],
        "allLeagues": ALL_LEAGUE_NAMES,
    })


@app.put("/api/general-bonuses")
def api_update_general_bonuses():
    payload = request.get_json(force=True) or {}
    categories = payload.get("categories")
    included_leagues = payload.get("includedLeagues")
    if not isinstance(categories, list) or not categories:
        return jsonify({"error": "categories manquant ou vide"}), 400
    if not isinstance(included_leagues, list):
        return jsonify({"error": "includedLeagues manquant"}), 400
    save_general_bonus_config(GENERAL_BONUS_CONFIG_PATH, categories, included_leagues)
    return jsonify({"ok": True})


@app.get("/api/multi-boss")
def api_multi_boss():
    return jsonify({"tables": load_multi_boss_config(MULTI_BOSS_CONFIG_PATH)})


@app.put("/api/multi-boss")
def api_update_multi_boss():
    payload = request.get_json(force=True) or {}
    tables = payload.get("tables")
    if not isinstance(tables, dict):
        return jsonify({"error": "tables manquant"}), 400
    save_multi_boss_config(MULTI_BOSS_CONFIG_PATH, tables)
    return jsonify({"ok": True})


if __name__ == "__main__":
    threading.Timer(1.0, lambda: webbrowser.open("http://127.0.0.1:5055")).start()
    # threaded=True -- sans ca, Flask dev server traite une requete a la fois ;
    # avec plusieurs matchs, chacun declenchant son propre /api/live-scenario-sweep
    # (fetch reseau + plusieurs secondes), les requetes se mettent en file et la
    # page semble figee en attendant qu'elles se vident une par une (retour
    # utilisateur 2026-08-10 : "la page bloque").
    app.run(host="127.0.0.1", port=5055, debug=False, threaded=True)
