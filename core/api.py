import sys
import time
from pathlib import Path
import requests

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import MPG_API
from core.token import load_token, decode_token_owner_id

TIMEOUT = 10

# Cache en memoire (process courant) pour get_championship_match -- retour
# utilisateur 2026-08-16 : /api/live-scenario, /api/live-sweep et
# /api/division-classement (branche "Mon Bonus") refetchent chacun,
# INDEPENDAMMENT, les memes ~10 vrais matchs a chaque interaction utilisateur
# (aucun cache avant ce correctif) -- mesure : 4.76s pour 10 fetch sequentiels,
# contre 0.01s pour 17 recalculs de score (compute_division_live_scores est
# du pur calcul, pas le goulot -- le reseau l'est). TTL court (le score/la
# minute d'un match REELLEMENT en direct doit rester a jour) mais suffisant
# pour dedupliquer les 2-3 endpoints qui se declenchent en rafale sur la MEME
# interaction (quelques secondes d'ecart, pas plus).
_CHAMPIONSHIP_MATCH_CACHE: dict[str, tuple[float, dict]] = {}
_CHAMPIONSHIP_MATCH_CACHE_TTL = 15.0


def build_headers(token: str = None) -> dict:
    if not token:
        token = load_token()
    return {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "fr-FR,fr;q=0.9",
        "Authorization": token,
        "Connection": "keep-alive",
        "Origin": "https://mpg.football",
        "Referer": "https://mpg.football/",
        "language": "fr-FR",
        "platform": "web",
        "client-version": "3.3.0",
    }


def test_token(token: str = None) -> tuple[bool, str]:
    """Returns (success, message)."""
    try:
        r = requests.get(f"{MPG_API}/dashboard", headers=build_headers(token), timeout=TIMEOUT)
        if r.status_code == 200:
            data = r.json()
            tiles = data.get("orderedTiles", [])
            return True, f"Token valide — {len(tiles)} ligue(s) trouvée(s)"
        elif r.status_code == 401:
            return False, "Token expiré ou invalide (401)"
        else:
            return False, f"Erreur {r.status_code}"
    except requests.exceptions.ConnectionError:
        return False, "Impossible de joindre l'API MPG"
    except Exception as e:
        return False, str(e)


def get_dashboard(token: str = None) -> dict:
    r = requests.get(f"{MPG_API}/dashboard", headers=build_headers(token), timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def get_championship_ids(token: str = None) -> dict[str, int]:
    """{shortId: championshipId} pour chaque tuile de type "league" du
    dashboard -- le dashboard peut contenir d'autres types de tuiles (ex.
    "SNCF Championship II" observe le 2026-08-14, pas une des 6 ligues
    suivies), filtrees ici. Sert a relier une ligue (League_Codes.json) au
    calendrier reel de get_nearest_game_weeks(), qui est indexe par
    championshipId, pas par shortId."""
    tiles = get_dashboard(token).get("orderedTiles", [])
    return {
        tile["shortId"]: tile["championshipId"]
        for tile in tiles
        if tile.get("type") == "league" and tile.get("shortId") and tile.get("championshipId") is not None
    }


def get_nearest_game_weeks(token: str = None) -> dict:
    """{championshipId (str): {"previousGameWeek": {...}, "nextGameWeek": {...}}}
    -- startDate/endDate (ISO UTC) et gameWeekNumber inclus pour chaque
    championnat REEL (football), tous suivis en une seule requete (retour
    utilisateur 2026-08-14 : "dans l'optique d'une automatisation totale...
    je pense que cette API aidera"). Sert a declencher live_watch.py
    automatiquement au vrai coup d'envoi plutot qu'a la main (cf.
    live_scheduler.py)."""
    r = requests.get(f"{MPG_API}/championship-calendars/nearest-game-weeks", headers=build_headers(token), timeout=TIMEOUT)
    r.raise_for_status()
    return r.json().get("championships", {})


def get_contact_book(user_id: str = None, token: str = None) -> list[dict]:
    """MPG n'a pas d'endpoint de recherche libre par nom -- /user/search renvoie
    404 (verifie 2026-07-27). Le seul moyen de trouver un manager est de lire le
    carnet de contacts (amis MPG) du compte connecte via /user/{id}/contact-book/details
    (endpoint trouve par l'utilisateur via DevTools). `user_id` par defaut = le
    proprietaire du token (decode depuis le JWT) -- MPG_Users.json['friends'] a
    d'ailleurs vraisemblablement ete peuple via ce meme endpoint (meme cle 'friends')."""
    token = token or load_token()
    user_id = user_id or decode_token_owner_id(token)
    if not user_id:
        raise ValueError("Impossible de determiner l'userId du token pour interroger le carnet de contacts.")

    r = requests.get(
        f"{MPG_API}/user/{user_id}/contact-book/details",
        headers=build_headers(token),
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    if isinstance(data, list):
        return data
    return data.get("friends", data.get("contacts", data.get("results", [])))


def get_user_profile(user_id: str, token: str = None) -> dict:
    r = requests.get(
        f"{MPG_API}/user/{user_id}",
        headers=build_headers(token),
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def get_division_info(short_id: str, season_number: int, division_number: int, token: str = None) -> dict:
    url = f"{MPG_API}/division/mpg_division_{short_id}_{season_number}_{division_number}"
    r = requests.get(url, headers=build_headers(token), timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def get_division_matches(short_id: str, season_number: int, division_number: int, game_week: int, token: str = None) -> list[dict]:
    """Matchs d'une division pour une journee -- composition MPG de chaque equipe
    (playersOnPitch = XI aux cles '1'-'11' + banc au-dela, captain, bonuses,
    tacticalSubs) et, par joueur, son matchId REEL (cf. get_championship_match).
    NE contient PAS le score/la note en direct malgre les apparences
    (home['score']/away['score'] restent a 0 et status a 1 meme match en cours,
    verifie 2026-08-08) -- le direct est uniquement dans get_championship_match.

    IMPORTANT (retour utilisateur 2026-08-18, verifie sur des vrais matchs) :
    match["status"] passe de 1 (encore provisoire, pas de note/bonusesDetails
    joueur meme APRES la fin du vrai match -- MPG met jusqu'a ~7h a tout
    stabiliser) a 2 avec match["finalResult"]=True une fois REELLEMENT
    termine cote MPG -- a ce moment-la seulement, chaque joueur porte
    "rating" (note finale) ET "bonusesDetails" (coups reellement joues),
    et team["badges"]/["bonuses"] sont a jour (Grotaldo, formation, etc.).
    Signal fiable pour savoir quand capturer l'archive "en dur" -- bien
    plus precis qu'un minuteur (cf. plan avant/apres, live_watch.py/
    live_scheduler.py)."""
    division_id = f"mpg_division_{short_id}_{season_number}_{division_number}"
    url = f"{MPG_API}/division/{division_id}/game-week/{game_week}/matches"
    r = requests.get(url, headers=build_headers(token), timeout=TIMEOUT)
    r.raise_for_status()
    return r.json().get("divisionMatches", [])


def get_division_live_ranks(short_id: str, season_number: int, division_number: int, token: str = None) -> dict[str, int]:
    """{teamId: rang} = classement COURANT de division tel qu'affiche par MPG
    lui-meme (liveState.standings), 7e niveau de departage (retour utilisateur
    2026-08-25, "si on arrive au bout des 6 criteres sans pouvoir departager
    les equipes, on se refere a l'API MPG" -- cf. core/internal_bonus.py::
    _resolve_tied_group). Verifie sur Rosbeef_League D8 : apres J1, 4 equipes
    (Cool Rasta/Fc pig/Piedcarre/Totof) restent MATHEMATIQUEMENT a egalite
    parfaite meme apres confrontation directe + buts exterieur (aucune n'a
    affronte toutes les autres, deux paires de matchs nuls identiques) -- MPG
    les departage quand meme via un critere non documente/non reproduit ici,
    d'ou ce repli sur SON classement plutot que d'inventer un 7e critere au
    hasard. Ne reflete que l'etat COURANT de MPG : n'a de sens que pour
    departager la journee la PLUS RECENTE connue (courante ou tout juste
    cloturee, tant qu'aucune journee suivante n'a demarre) -- jamais pour
    corriger une journee archivee plus ancienne lors d'un rebuild complet."""
    division_id = f"mpg_division_{short_id}_{season_number}_{division_number}"
    url = f"{MPG_API}/division/{division_id}"
    r = requests.get(url, headers=build_headers(token), timeout=TIMEOUT)
    r.raise_for_status()
    standings = r.json().get("liveState", {}).get("standings", {}) or {}
    return {team_id: s["rank"] for team_id, s in standings.items() if "rank" in s}


def get_division_calendar(short_id: str, season_number: int, division_number: int, token: str = None) -> dict:
    """Calendrier COMPLET d'une division pour une saison (endpoint trouve dans
    MPG_Ligue_2_EKT_Test_2025.ipynb/Super_Classement_General_V2.ipynb, jamais
    utilise nulle part dans ce code avant ce correctif -- retour utilisateur
    2026-08-18). Renvoie {"fixtures": [{"gameWeek", "realGameWeek",
    "matchesIds", "previousTargetMan", "afterTargetMan"}, ...]} -- UNE
    entree par journee de TOUTE la saison en un seul appel (verifie : 14
    fixtures d'un coup sur une saison complete, pas besoin de repeter
    l'appel par journee). "previousTargetMan"/"afterTargetMan" sont des
    TEAM ID (mpg_team_..., PAS des userId) -- a mapper via
    get_division_matches (home/away["teamId"]/["userId"] de la meme
    division/saison) -- absents pour les journees dont le vrai resultat
    n'est pas encore confirme par MPG (meme delai que get_division_matches,
    cf. sa docstring). Seule source fiable du "Precieux" (afterTargetMan) --
    PAS dans get_division_matches, contrairement au reste (score/badges/
    bonusesDetails/remplacements)."""
    division_id = f"mpg_division_{short_id}_{season_number}_{division_number}"
    url = f"{MPG_API}/division/{division_id}/calendar"
    r = requests.get(url, headers=build_headers(token), timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def get_division_teams(short_id: str, season_number: int, division_number: int, token: str = None) -> list[dict]:
    """Equipes d'une division (nom, abreviation, budget restant, effectif...),
    via /teams/division/{divisionId}. C'est le SEUL endroit ou MPG expose le
    nom d'equipe -- /division/mpg_division_{...} et /league/{leagueId} ne
    l'ont jamais."""
    division_id = f"mpg_division_{short_id}_{season_number}_{division_number}"
    r = requests.get(f"{MPG_API}/teams/division/{division_id}", headers=build_headers(token), timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def get_division_team_names(short_id: str, season_number: int, division_number: int, token: str = None) -> dict[str, str]:
    """{userId: nom d'equipe} pour UNE division -- combine usersTeams
    (get_division_info, userId->teamId) et /teams/division/{id} (nom,
    get_division_teams). Version scopee a une seule division de
    mpg_app::get_league_team_info (qui balaie toute la ligue) -- ici
    scripts/live_job.py connait deja la division a chaque appel, pas besoin
    de re-parcourir les autres. Sert de nom affiche en v1 (pas de registre
    managers/RealName cote mpg_live, cf. core/live_projection.py)."""
    info = get_division_info(short_id, season_number, division_number, token=token)
    users_teams = info.get("usersTeams") or {}
    team_id_to_user = {team_id: user_id for user_id, team_id in users_teams.items()}
    try:
        teams = get_division_teams(short_id, season_number, division_number, token=token)
    except requests.exceptions.RequestException:
        teams = []
    result = {}
    for team in teams:
        user_id = team_id_to_user.get(team.get("id"))
        if user_id:
            result[user_id] = team.get("name", "")
    return result


def get_division_match_detail(match_id: str, token: str = None) -> dict:
    """Detail complet d'UN match de division (endpoint trouve par l'utilisateur,
    2026-08-10 : /division-match/{matchId}, distinct de get_division_matches qui
    ne donne que la liste + bonuses/badges au niveau EQUIPE). Donne notamment
    home/away["players"][playerId]["bonusesDetails"] -- bonus specifiques recus
    par CE joueur dans CE match (captain, boostDefense4/5, mais aussi les bonus
    "attaques" par l'adversaire type nerfGoalkeeper/Suarez, mandatorySubstitution,
    etc.), la ou get_division_matches ne donne qu'un flag au niveau equipe sans
    dire QUI l'a recu. Fonctionne aussi sur de vieux matchs clos (verifie sur
    une saison 2023-2024)."""
    r = requests.get(f"{MPG_API}/division-match/{match_id}", headers=build_headers(token), timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def get_championship_match(match_id: str, token: str = None) -> dict:
    """Etat REEL d'un match de championnat (endpoint trouve en observant un match
    en direct, 2026-08-08) : period ("firstHalf"/...), matchTime ("23'"), score
    reel home/away, et par joueur mpgRating (note live) + score (points fantasy
    detailles) + stats. `match_id` vient de get_division_matches
    (players[playerId]['matchId']). Mis en cache _CHAMPIONSHIP_MATCH_CACHE_TTL
    secondes (cf. sa docstring) -- appelant qui a besoin d'un etat garanti frais
    (rare) peut vider _CHAMPIONSHIP_MATCH_CACHE.pop(match_id, None) avant
    d'appeler."""
    now = time.monotonic()
    cached = _CHAMPIONSHIP_MATCH_CACHE.get(match_id)
    if cached and now - cached[0] < _CHAMPIONSHIP_MATCH_CACHE_TTL:
        return cached[1]
    r = requests.get(f"{MPG_API}/championship-match/{match_id}", headers=build_headers(token), timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    _CHAMPIONSHIP_MATCH_CACHE[match_id] = (now, data)
    return data


def get_league_join_status(short_id: str, token: str = None) -> dict[str, set[str]]:
    """Etat d'inscription d'une ligue pour la saison en cours, en un seul appel
    /league/{leagueId} :
    - "pool" : le vivier historique de la ligue (champ racine `usersIds`) -- tous
      les userId ayant deja fait partie de cette ligue par le passe, a peu pres
      stable dans le temps (~71-72 sur les ligues observees), PAS specifique a la
      saison en cours.
    - "joined" : userId deja repartis dans un groupe/division pour la saison en
      cours (`divisions[*].usersIds`) -- correspond exactement au `totalUsers` du
      dashboard MPG (verifie : 24 pour Camembert, 12 pour Liga_Tapas). Fonctionne
      meme en phase pre-mercato, avant que /division/mpg_division_{...} n'existe
      (404 tant que le mercato n'a pas demarre, verifie 2026-08-04).
    - "not_yet_joined" : pool - joined -- a deja joue cette ligue mais pas encore
      rejoint/groupe cette saison, la vraie cible de relance (contrairement a une
      comparaison contre TOUT MPG_Users.json, qui inclut des managers qui ne
      jouent simplement jamais cette ligue).

    A NE PAS CONFONDRE avec /league/{leagueId}/users (endpoint different), qui
    renvoie le meme type d'historique complet sous une autre forme -- inutile ici,
    `usersIds` de /league/{leagueId} suffit."""
    league_id = f"mpg_league_{short_id}"
    r = requests.get(f"{MPG_API}/league/{league_id}", headers=build_headers(token), timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()

    pool = set(data.get("usersIds") or [])
    joined: set[str] = set()
    for division in (data.get("divisions") or {}).values():
        joined.update(division.get("usersIds", []))

    return {"pool": pool, "joined": joined, "not_yet_joined": pool - joined}


def get_league_team_ids(short_id: str, season_number: int, token: str = None, max_divisions: int = 30) -> dict[str, str]:
    """Union des {userId: teamId} de toutes les divisions d'une ligue/saison, en
    direct sur MPG (`usersTeams` de chaque division, cf. get_division_info). Ne
    fonctionne qu'une fois le mercato terminé -- avant ça, les divisions de match
    n'existent pas encore (404, cf. get_league_join_status pour l'inscription en
    phase pré-mercato). S'arrete a la premiere division qui echoue ou est vide, en
    supposant une numerotation continue a partir de 1."""
    team_ids: dict[str, str] = {}
    for division in range(1, max_divisions + 1):
        try:
            info = get_division_info(short_id, season_number, division, token=token)
        except requests.exceptions.RequestException:
            break
        users_teams = info.get("usersTeams") or {}
        if not users_teams:
            break
        team_ids.update(users_teams)
    return team_ids


def get_league_team_info(short_id: str, season_number: int, token: str = None, max_divisions: int = 30) -> dict[str, dict]:
    """{userId: {teamId, name, abbreviation, budget, squadSize}} pour toute une
    ligue/saison -- combine `usersTeams` (get_division_info, userId->teamId) et
    /teams/division/{id} (nom + infos mercato, get_division_teams) division par
    division. S'arrete a la premiere division qui echoue ou est vide (mercato pas
    termine, ou fin de la ligue)."""
    result: dict[str, dict] = {}
    for division in range(1, max_divisions + 1):
        try:
            info = get_division_info(short_id, season_number, division, token=token)
        except requests.exceptions.RequestException:
            break
        users_teams = info.get("usersTeams") or {}
        if not users_teams:
            break
        team_id_to_user = {team_id: user_id for user_id, team_id in users_teams.items()}

        try:
            teams = get_division_teams(short_id, season_number, division, token=token)
        except requests.exceptions.RequestException:
            teams = []

        for team in teams:
            user_id = team_id_to_user.get(team.get("id"))
            if not user_id:
                continue
            result[user_id] = {
                "teamId": team.get("id"),
                "name": team.get("name", ""),
                "abbreviation": team.get("abbreviation", ""),
                "budget": team.get("budget"),
                "squadSize": len(team.get("squad") or {}),
            }
    return result
