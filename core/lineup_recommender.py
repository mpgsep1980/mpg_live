"""
Recommandation de composition (XI + banc + capitaine) pour l'onglet "Compo" --
retour utilisateur 2026-08-27, "serait-il possible de donner des instructions
a l'appli pour faire une compo ... et de l'envoyer vers MPG pour qu'elle se
fasse seule". Decision explicite apres discussion (risque CGU d'automatiser
une action de jeu sur un tiers) : PAS d'ecriture vers MPG -- l'app calcule et
affiche une recommandation, le manager la recopie lui-meme dans la vraie
appli MPG. Fonctions pures ici, aucune I/O -- l'appelant (simulate_api/
app.py) fournit l'effectif (core.api.get_division_teams) et le pool de
joueurs (core.api.get_player_pool) deja recuperes.

V1 : XI + banc + capitaine seulement. Bonus et seuils de remplacement
tactique reportes a une iteration suivante (raisonnement different, plus
heuristique -- pas necessaire pour une premiere version utile).

V1.1 (retour utilisateur 2026-08-29, apres premier test reel) : le pool
MPG (championship-players-pool) n'expose AUCUN champ blessure/suspension
EXPLICITE -- mais retour utilisateur suivant, avec le HTML du vrai site
MPG a l'appui (icone croix rouge sur "Ochoa", pas "Dotor" comme d'abord
suppose) : stats.nearestMatches.matches[].status DEJA present dans les
donnees qu'on recupere (aucun nouvel appel reseau requis) encode bien un
signal exploitable. Verifie sur l'integralite d'un pool reel (471
joueurs, Liga_Tapas) : status 1/2 = a joue (rating+goalsScored presents),
3/4 = dans le groupe mais pas rentre (goalsScored seul, PAS blesse -- un
simple choix tactique), 6 = ABSENT du groupe du match (ni rating ni
goalsScored) -- correlation 100% propre sur les 5 valeurs observees, et
confirme sur Ochoa (status=6 sur ses 2 derniers matchs, blessure
confirmee par l'utilisateur). is_likely_unavailable() s'appuie sur ce
signal pour ecarter ces joueurs des choix automatiques -- mais
full_squad() les expose quand meme (avec le flag) pour rester
selectionnables a la main, ce champ restant un signal RETROSPECTIF
(dernier match joue), pas une confirmation temps reel pour le prochain
match.
"""

# Formations MPG reelles, echantillonnees sur des divisions en direct
# (aucune doc officielle, comme le reste de cette couche API reverse-
# engineered) -- {position: nombre de titulaires a ce poste}, 1=G/2=D/3=M/4=A
# (meme convention que core/live_scoring.py), toujours 11 au total.
FORMATIONS: dict[str, dict[int, int]] = {
    "442": {1: 1, 2: 4, 3: 4, 4: 2},
    "433": {1: 1, 2: 4, 3: 3, 4: 3},
    "343": {1: 1, 2: 3, 3: 4, 4: 3},
    "352": {1: 1, 2: 3, 3: 5, 4: 2},
    "532": {1: 1, 2: 5, 3: 3, 4: 2},
    "541": {1: 1, 2: 5, 3: 4, 4: 1},
}

# En dessous de ce nombre d'APPARITIONS reelles (titulaire OU entre en cours
# de jeu), un joueur est exclu de la recommandation (aucune moyenne fiable).
# totalPlayedMatches, PAS totalStartedMatches (retour utilisateur 2026-08-27,
# teste sur Lega_Calzone J2 : Sandro Kulenovic a une vraie moyenne
# averagePoints=4.37 mais 0 titularisation, seulement entre en cours de jeu
# -- totalStartedMatches l'excluait a tort, et en tres debut de saison
# suffisamment de joueurs sont dans ce cas pour qu'AUCUNE formation ne
# puisse plus etre completee, ex. effectif reel avec seulement 1 attaquant
# "titulaire" utilisable sur tout un effectif).
MIN_PLAYED_MATCHES = 1


# status du dernier match dans stats.nearestMatches.matches signifiant
# "absent du groupe" (ni note ni but marque, contrairement a 1/2 = a joue
# et 3/4 = dans le groupe mais pas rentre) -- cf. docstring module.
UNAVAILABLE_MATCH_STATUS = 6


def last_played_match_status(pool_entry: dict) -> int | None:
    """Status MPG du DERNIER match deja joue (pas le prochain, qui n'a pas
    encore de `status`) -- None si aucun match joue n'est connu (nouveau
    joueur)."""
    matches = ((pool_entry.get("stats") or {}).get("nearestMatches") or {}).get("matches") or []
    played = [m for m in matches if "status" in m]
    if not played:
        return None
    played.sort(key=lambda m: m.get("gameWeekNumber", 0))
    return played[-1]["status"]


def is_likely_unavailable(pool_entry: dict) -> bool:
    """True si le joueur n'etait meme pas dans le groupe convoque lors de
    son dernier match (le plus souvent blessure/suspension -- cf.
    docstring module, verifie sur un cas reel signale par l'utilisateur,
    "Ochoa"). Signal retrospectif, pas une confirmation pour le PROCHAIN
    match -- utilise pour ecarter ces joueurs des choix automatiques,
    jamais pour les cacher (cf. full_squad)."""
    return last_played_match_status(pool_entry) == UNAVAILABLE_MATCH_STATUS


def player_projection(pool_entry: dict) -> float | None:
    """Projection simple pour UN joueur du pool -- stats.averagePoints (la
    metrique MPG elle-meme, la plus directement comparable a ce qui compte
    reellement pour le classement), None si trop peu d'apparitions reelles
    pour etre fiable (cf. MIN_PLAYED_MATCHES) ou si le pool n'a aucune stat
    pour lui (nouveau joueur, jamais joue). Pas de ponderation
    par forme recente (stats.lastRatings) ni par difficulte du prochain
    adversaire (stats.nextMatch.preGameQuotations) en V1 -- pistes de
    raffinement futures, pas bloquantes pour une premiere version utile."""
    stats = pool_entry.get("stats") or {}
    played = stats.get("totalPlayedMatches") or 0
    if played < MIN_PLAYED_MATCHES:
        return None
    points = stats.get("averagePoints")
    return float(points) if points is not None else None


def full_squad(squad_ids: list[str], pool: dict[str, dict]) -> list[dict]:
    """TOUS les joueurs POSSEDES, y compris ceux sans projection fiable
    (cf. MIN_PLAYED_MATCHES -- projection None plutot qu'exclus). Sert a
    l'edition manuelle cote frontend (retour utilisateur 2026-08-29,
    "les joueurs sans notes n'apparaissent pas" -- recommend_lineup() ne
    renvoie que les joueurs assez fiables pour etre recommandes, mais un
    manager doit pouvoir titulariser N'IMPORTE quel joueur possede, y
    compris un nouvel arrivant ou un joueur jamais titularise)."""
    squad = []
    for pid in squad_ids:
        entry = pool.get(pid)
        if not entry:
            continue
        projection = player_projection(entry)
        squad.append({
            "playerId": pid,
            "position": entry.get("position"),
            "projection": round(projection, 2) if projection is not None else None,
            "likelyUnavailable": is_likely_unavailable(entry),
        })
    return squad


def recommend_lineup(squad_ids: list[str], pool: dict[str, dict]) -> dict | None:
    """`squad_ids` : ids des joueurs POSSEDES par le manager (cf.
    core.api.get_division_teams -- team["squad"].keys()). `pool` :
    {playerId: poolEntry} (cf. core.api.get_player_pool) pour le
    championnat reel de cette ligue.

    Pour chaque formation de FORMATIONS, tente de la remplir en choisissant,
    poste par poste, les joueurs POSSEDES a la plus haute projection PARMI
    CEUX PROBABLEMENT DISPONIBLES (is_likely_unavailable exclu -- jamais
    recommande par defaut, mais reste choisissable a la main via
    full_squad) -- formation ecartee (pas d'erreur) si l'effectif ne peut
    pas la completer (ex. un seul gardien blesse/jamais titularise). Retient la formation
    dont la somme des projections titulaires est la plus haute parmi celles
    realisables. Renvoie {formation, starters: [{playerId, position,
    projection}], bench: [{playerId, position, projection}], captainId}
    trie par projection decroissante au sein de chaque poste/du banc --
    None si AUCUNE formation n'est realisable avec cet effectif."""
    by_position: dict[int, list[tuple[str, float]]] = {1: [], 2: [], 3: [], 4: []}
    for pid in squad_ids:
        entry = pool.get(pid)
        if not entry:
            continue
        projection = player_projection(entry)
        if projection is None or is_likely_unavailable(entry):
            continue
        position = entry.get("position")
        if position in by_position:
            by_position[position].append((pid, projection))
    for position in by_position:
        by_position[position].sort(key=lambda pair: pair[1], reverse=True)

    best_formation: str | None = None
    best_starters: dict[str, tuple[str, float]] = {}
    best_total = float("-inf")

    for formation, counts in FORMATIONS.items():
        feasible = True
        starters: dict[str, tuple[str, float]] = {}
        for position, count in counts.items():
            available = by_position.get(position, [])
            if len(available) < count:
                feasible = False
                break
            for pid, projection in available[:count]:
                starters[pid] = (formation, projection)
        if not feasible:
            continue
        total = sum(projection for _, projection in starters.values())
        if total > best_total:
            best_total = total
            best_formation = formation
            best_starters = starters

    if best_formation is None:
        return None

    starter_ids = set(best_starters.keys())
    starters_out = sorted(
        (
            {"playerId": pid, "position": pool[pid]["position"], "projection": round(proj, 2)}
            for pid, (_, proj) in best_starters.items()
        ),
        key=lambda p: (-p["projection"]),
    )
    captain_id = max(best_starters.items(), key=lambda item: item[1][1])[0]

    bench = []
    for pid in squad_ids:
        if pid in starter_ids or pid not in pool:
            continue
        projection = player_projection(pool[pid])
        if projection is None or is_likely_unavailable(pool[pid]):
            continue
        bench.append({"playerId": pid, "position": pool[pid]["position"], "projection": round(projection, 2)})
    bench.sort(key=lambda p: -p["projection"])

    return {
        "formation": best_formation,
        "starters": starters_out,
        "bench": bench,
        "captainId": captain_id,
    }
