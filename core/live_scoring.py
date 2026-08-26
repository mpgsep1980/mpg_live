"""
Score live d'un match de division MPG (note + bonus), a partir de
core.api.get_division_matches() (composition MPG : XI, capitaine, bonus, subs
tactiques, matchId reel de chaque joueur) + core.api.get_championship_match()
(etat reel du match : mi-temps/minute, score club, note/score MPG en direct).

Regles de bonus :
- Capitaine : +0.5 a la note -- pas suppose, lu directement sur
  team["bonuses"]["captain"]["bonusRating"] (confirme == 0.5 en observant l'API
  le 2026-08-08).
- Nombre de defenseurs dans la formation (team["composition"], ex. 343/442/541
  -- 1er chiffre = nb de DF) : 3 DF -> note normale, 4 DF -> +0.5 pour TOUS les
  defenseurs titulaires, 5 DF -> +1 pour tous les defenseurs titulaires
  (confirme par l'utilisateur 2026-08-08 -- PAS conditionne par une feuille
  blanche ni par la note individuelle du joueur, uniquement par la taille de
  la ligne defensive choisie par le manager). Corrige une premiere
  implementation erronee de ma part qui avait suppose a tort un bonus feuille
  blanche indexe sur la note du defenseur (memes valeurs 4/5 -> 0.5/1, mais
  mauvais declencheur) -- FORMATION_DEFENDER_BONUS_TIERS remplace
  CLEAN_SHEET_DEFENDER_BONUS_TIERS. S'applique a tout defenseur qui finit dans
  le XI effectif (titulaire d'origine OU entre par un remplacement, cf. plus
  bas) -- calcule des le depart (compute_player_live_score) puisque la
  formation ne depend pas de qui joue effectivement.

Hors scope pour l'instant : subs tactiques (team["tacticalSubs"]) -- la regle
MPG les evalue a la note FINALE du titulaire (fin de match), pas en continu ;
en cours de match, compute_team_live_score renvoie donc un total PROVISOIRE
avec les titulaires tels quels, sans appliquer de remplacement.

--- "Duel de lignes" (regle maison, pas du MPG officiel) ---------------------

Chaque ligne de joueurs (GK/DF/MF/FW) affronte les lignes adverses qu'elle a
"devant elle", dans l'ordre, en partant de sa propre note (bonus deja compris,
cf. plus haut) :
- Attaquant (FW)   : DF adverse, puis GK adverse (2 etapes).
- Milieu (MF)      : MF adverse, DF adverse, puis GK adverse (3 etapes).
- Defenseur (DF)   : FW adverse, MF adverse, DF adverse, puis GK adverse
  (4 etapes -- doit traverser tout le bloc adverse avant de defier le gardien).
- Gardien (GK)     : ne tente jamais rien (un gardien ne peut pas marquer de
  but MPG) -- note inchangee.

A chaque etape, le joueur "passe" la ligne si sa note (deja reduite par les
etapes precedentes, cf. ci-dessous) est STRICTEMENT superieure a la moyenne
des notes de la ligne adverse en face (moyenne d'un GK seul = sa propre note).
En cas d'egalite : le joueur passe si son EQUIPE FANTASY joue a domicile dans
ce match de division, sinon il ne passe pas. Des qu'une etape echoue, la
progression s'arrete (les etapes suivantes ne sont pas tentees).

Cout : la 1ere ligne passee coute -1 a la note du joueur, chaque ligne
suivante passee coute -0.5 -- applique immediatement, donc la comparaison de
l'etape suivante se fait avec la note deja reduite (progression cumulative).

Recompense : si un joueur passe TOUTES ses lignes (gardien adverse compris)
ET a une note EFFECTIVE (bonus compris, avant toute penalite de duel) d'AU
MOINS 5/10 -- regle officielle MPG R1, retour utilisateur 2026-08-18 confirme
avec Ilan, absente avant ce correctif -- il marque un "but MPG" pour son
EQUIPE -- pas de bareme de points invente ici (pas de "but classique" : les
seuls buts avec un bareme sont les vrais buts marques dans les matchs reels,
deja captes par mpgRating cf. plus haut). Le but MPG est donc juste compte
(team["buts_mpg"]), pas ajoute a la note du joueur -- seule la penalite de la
derniere ligne (le duel contre le gardien) s'applique a sa note, comme
n'importe quelle autre ligne franchie. EXCEPTION : un joueur ayant deja
marque un but REEL dans son match (stats.goals >= 1, cf.
get_championship_match) ne peut pas marquer de but MPG en plus -- il "passe"
quand meme la ligne du gardien (penalite appliquee comme d'habitude), seul le
comptage du but MPG est retire.

Important : la moyenne de chaque ligne adverse est calculee sur les notes
AVANT duel de lignes (cf. compute_team_live_score) -- fige des le depart pour
les deux equipes, pour eviter un ordre de calcul qui avantagerait une equipe
sur l'autre (les deux duels sont simultanes, pas sequentiels).

--- Remplacements (regle MPG officielle, cf. reglement fourni 2026-08-08) -----

Applique AVANT le duel de lignes -- c'est la note effective post-remplacement
(effective_note) qui sert de point de depart au duel, pas note_finale (qui
reste expose tel quel, c'est la note du titulaire D'ORIGINE avec ses propres
bonus, pour tracer QUI a ete remplace et pourquoi).

Deux mecanismes, le tactique prioritaire sur l'obligatoire :

1. Remplacement tactique (jusqu'a 5, team["tacticalSubs"] : [{starterId, subId,
   rating}]) -- jamais pour un gardien. Se declenche si note_finale du
   titulaire (bonus deja compris, y compris capitaine) est < `rating`, ou s'il
   n'a pas joue (note_finale None compte comme "en dessous de tout seuil") --
   MAIS seulement si le remplacant designe a lui-meme joue (minutes_played >
   0). S'il n'a pas joue, il n'y a personne a faire entrer : le remplacement
   tactique ne se fait PAS, le titulaire reste en place tel quel (confirme par
   l'utilisateur 2026-08-08, cf. division 13/Tia -- le risque du tactique,
   c'est une note moins bonne chez le remplacant, pas son absence de jeu).
   Le remplacant entre AVEC SA PROPRE note (pas de penalite de poste -- la
   regle ne le prevoit pas pour le tactique). Un meme remplacant ne peut pas
   couvrir 2 titulaires (premier arrive gagne, on traite les 11 titulaires
   dans l'ordre des slots).

2. Remplacement obligatoire -- seulement pour les titulaires NON couverts par
   un tactique et qui n'ont pas joue (note_finale/minutes_played == 0) :
   cherche sur le banc, DANS L'ORDRE (cle numerique du slot), le premier
   joueur du MEME poste ayant joue et pas deja utilise. A defaut, cascade vers
   le(s) poste(s) INFERIEUR(S) un par un (MF -> DF, FW -> MF -> DF) -- jamais
   vers un poste superieur, jamais vers/depuis le gardien. Chaque "poste
   saute" coute -1 a la note du remplacant. Si rien n'est trouve (aucun
   candidat du bon cote n'a joue, y compris apres cascade jusqu'a DF) :
   Rotaldo (note fixe 2.5/10).
   HYPOTHESE NON CONFIRMEE : que fait-on si un DEFENSEUR titulaire n'a pas
   joue et qu'aucun defenseur de banc n'a joue non plus ? Le reglement ne
   parle que de cascade vers l'inferieur (DF etant deja le plancher hors
   gardien) -- j'ai suppose Rotaldo direct dans ce cas (pas de bascule vers un
   milieu), a confirmer.

   IMPORTANT (bug corrige 2026-08-15) : "n'a pas joue" ne veut dire "absent
   pour de bon" QUE si son VRAI match est TERMINE (real_match["period"] in
   FINISHED_MATCH_PERIODS, cf. compute_player_live_score -> match_finished).
   Une journee MPG etale ses vrais matchs sur plusieurs jours (ex. Liga_Tapas
   J1, 2026-08-15 : matchs du 15 au 27 aout) -- tant que le match d'un
   titulaire n'a pas commence OU est encore en cours, 0 minute ne prouve rien
   (il peut entrer plus tard, ou son match n'a simplement pas encore eu lieu).
   Avant ce correctif, la passe 2 traitait TOUT le monde comme confirme absent
   des la minute 1 de la journee -- Rotaldo en cascade sur la quasi-totalite
   du XI de tous les managers, des scores de duel de lignes incoherents des
   l'ouverture de la fenetre MPG (retour utilisateur : "Les scores sont
   delirants"). Un titulaire pas encore fixe (match pas fini) est desormais
   "en attente" : ni substitue, ni Rotaldo, effective_note=None (exclu du
   total provisoire et du duel de lignes jusqu'a ce que son vrai match se
   termine).

Capitaine : le bonus capitaine (+0.5, deja dans note_finale du titulaire
d'origine) n'est PAS transfere si le capitaine est remplace (tactique ou
obligatoire) -- il est juste perdu pour l'equipe ce match-la (regle explicite
: "tu peux faire sortir ce joueur, tant pis pour ses +0.5pt").

Rotaldo / CSC : chaque slot en Rotaldo compte dans team["rotaldo_count"].
Tous les 3 Rotaldo dans le XI final, l'equipe encaisse un CSC (but contre son
camp) -- implemente comme +1 au SCORE DE L'ADVERSAIRE (team["csc_conceded"] =
rotaldo_count // 3), coherent avec le sens reel de "CSC" (but marque pour
l'autre equipe), ajoute au tableau de bord (apply_line_battles). Ce comptage
est CONFIRME (chaque Rotaldo qui y contribue n'est retenu qu'une fois son
vrai match TERMINE, cf. "IMPORTANT" ci-dessus) -- correspond au badge
officiel MPG "bigRotaldo"/Grotaldo, revele par MPG seulement a la fin du
match reel (jamais en direct, meme contrainte que team["bonusesDetails"]).

PAS de mecanisme "Grotaldo provisoire" separe (retire le 2026-08-17) : un
ancien heuristique comptait tout titulaire D'ORIGINE encore a 0 minute des
que son vrai match avait demarre, MEME quand un remplacant tactique/
obligatoire valide avait deja couvert son poste (donc un joueur bel et bien
SUR LE TERRAIN a ce slot) -- retour utilisateur : "Seuls les joueurs sur le
terrain comptent comme un grotaldo... si dans le scenario actuel il n'y a
que 2 Rotaldo sur le terrain alors pas de CSC Grotaldo". rotaldo_count
(ci-dessus) EST deja ce "scenario a l'instant T" -- deja provisoire par
nature (augmente au fil des vrais matchs qui se terminent), seule source de
verite desormais.

Hors scope pour l'instant : "Option 2, remplacements live week-end" (mode
alternatif choisi par l'admin de ligue a la place du tactique -- cf.
team["isLiveSubstitutesEnabled"], observe a False sur toutes les equipes
inspectees jusqu'ici -- necessiterait de connaitre l'effectif complet + les
heures de coup d'envoi de CHAQUE joueur, pas encore explore).

--- Coups (bonus qui changent le cours du jeu, declares par le manager) ------

Applique ICI (compute_team_live_score, cf. plus bas) APRES resolution des
remplacements et AVANT le duel de lignes -- confirme par l'utilisateur
2026-08-09 : ces bonus doivent pouvoir faire franchir une ligne en plus/en
moins, comme le bonus capitaine/formation deja en place, pas juste ajuster le
total final.

IMPORTANT (retour utilisateur 2026-08-10) : on n'a AUCUN moyen fiable de savoir
quel coup un manager a reellement joue (pas expose par l'API MPG) -- donc ces
fonctions ne sont JAMAIS appelees depuis le calcul live "officiel"
(live_watch.py ne leur passe rien). Elles ne servent que pour la simulation a
la demande (/api/live-scenario, non persistee, jamais partagee entre managers
-- afficher un coup non confirme dans le match REEL donnerait une indication
injuste a l'adversaire qui consulterait sa propre page).

Phase 1 implementee (modificateurs de note simples, pas d'interaction avec
remplacements/buts/autres bonus) -- cles identiques a BONUS_LABELS
(core/scoring.py) pour rester coherent avec le suivi des badges MPG :
- boostAllPlayers (Zahia) : +0.5 a TOUS les titulaires alignes de sa propre
  equipe (gardien compris, aucune exclusion mentionnee dans le reglement) --
  EXCLUT les remplacants (tactique ou obligatoire, cf. apply_own_bonus_effect
  -- retour utilisateur 2026-08-17 : "Une zahia s'applique uniquement aux
  titulaires", confirme impossible autrement).
- boostOnePlayer (McDo+) : +1 a UN titulaire choisi par le manager
  (targetPlayerId, doit faire partie du XI final).
- nerfGoalkeeper (Suarez) : -1 au gardien de l'equipe ADVERSE.
- nerfAllPlayers (Cheat Code 18-26) : -0.5 a chaque joueur de champ (hors
  gardien) de l'equipe ADVERSE, sur le XI final post-remplacements (coherent
  avec le reglement : "applique apres les remplacements").

Phase 2 (2026-08-10) -- bonus qui ne sont PAS de simples modificateurs de
note, cf. resolve_match_bonus_effects/apply_removed_goal plus bas :
- blockTacticalSubs (Tonton Pat') : bloque la resolution tactique (passe 1 de
  resolve_starting_lineup) de l'equipe ADVERSE -- seulement les remplacements
  PAS ENCORE actes par MPG cote serveur (cf. "IMPORTANT" plus haut : en direct,
  MPG ne resout AUCUN remplacement lui-meme, il ne le fait qu'une fois le
  match REEL termine -- confirme par l'utilisateur 2026-08-10 -- donc bloquer
  notre propre resolution simulee suffit, rien a annuler apres coup). Les
  remplacements obligatoires (passe 2) restent actifs, comme specifie dans le
  reglement.
- removeGoal (Valise a Nanard) : annule le PREMIER but adverse chronologiquement
  (reel ou MPG), priorite de ligne DF > MF > FW, jamais un CSC -- applique
  APRES apply_line_battles (les buts doivent etre determines). Compensation
  5M€ non modelisee (budget hors scope du calcul de score).
- mirror (Miroir) : redirige l'effet du coup adverse plutot que d'agir
  lui-meme -- cf. resolve_match_bonus_effects. Les bonus "self" (Zahia/McDo+)
  sont VOLES par le lanceur du Miroir ; pour McDo+, la cible n'est PAS choisie
  a la main -- c'est le titulaire du lanceur du Miroir occupant le MEME
  NUMERO DE SLOT dans sa compo que le titulaire vise chez l'adversaire (cf.
  _slot_of_player/_player_in_slot, retour utilisateur 2026-08-10). Les bonus
  "opponent" (Suarez/Cheat Code/Tonton Pat'/Valise) sont RENVOYES contre leur
  lanceur original. Si les deux managers jouent Miroir simultanement, aucun
  effet a refleter (no-op).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

ROTALDO_NOTE = 2.5

# period terminal cote get_championship_match -- "fullTime" confirme par
# l'observation (2026-08-15), les deux autres sont une hypothese pour les
# matchs a prolongation (Ligue des Champignons uniquement, pas encore
# observe en vrai) -- a corriger si un jour un ecart est constate.
FINISHED_MATCH_PERIODS = {"fullTime", "afterExtraTime", "afterPenalties"}

# Valeurs de "period" observees AVANT le coup d'envoi -- retour utilisateur
# 2026-08-23 : Lega_Calzone D12 J1, le gardien adverse (Vicario, vrai match
# pas encore commence) avait "period": "preMatch", une valeur NON-None que
# real_match_started traitait a tort comme "deja demarre" (l'hypothese
# d'origine etait que period reste absent/None jusqu'au coup d'envoi, cf.
# docstring compute_player_live_score) -- sa note restait donc None au lieu
# de la note par defaut 5 (meme regle que tout titulaire pas encore joue),
# ce qui bloquait la ligne de gardien adverse ENTIERE (moyenne introuvable,
# cf. _line_average) et empechait tout attaquant en face de terminer son
# duel de lignes jusqu'au bout -- 2 buts MPG manquants (Adam Obert, Andy
# Diouf, tous deux a egalite exacte 5.0 contre la note par defaut du
# gardien, tranchee en faveur du domicile).
NOT_STARTED_MATCH_PERIODS = {"preMatch"}

POSITION_GOALKEEPER = 1
POSITION_DEFENDER = 2
POSITION_MIDFIELDER = 3
POSITION_FORWARD = 4

# Lignes adverses a affronter, dans l'ordre, par poste du joueur qui attaque.
LINE_SEQUENCE_BY_POSITION = {
    POSITION_FORWARD: [POSITION_DEFENDER, POSITION_GOALKEEPER],
    POSITION_MIDFIELDER: [POSITION_MIDFIELDER, POSITION_DEFENDER, POSITION_GOALKEEPER],
    POSITION_DEFENDER: [POSITION_FORWARD, POSITION_MIDFIELDER, POSITION_DEFENDER, POSITION_GOALKEEPER],
}

# nb de defenseurs dans la formation -> bonus pour CHAQUE defenseur titulaire.
# Cf. docstring module pour la provenance/le declencheur (formation, pas note).
FORMATION_DEFENDER_BONUS_TIERS = {3: 0.0, 4: 0.5, 5: 1.0}


def formation_defender_count(composition) -> int | None:
    """1er chiffre de team["composition"] (ex. 343 -> 3, 442 -> 4, 541 -> 5) --
    None si absent/format inattendu (pas de bonus applique dans ce cas)."""
    if not composition:
        return None
    try:
        return int(str(int(composition))[0])
    except (ValueError, TypeError):
        return None


def formation_defender_bonus(position: int | None, composition) -> float:
    """Bonus de note lie au nombre de defenseurs de la formation -- uniquement
    pour les defenseurs (cf. docstring module)."""
    if position != POSITION_DEFENDER:
        return 0.0
    count = formation_defender_count(composition)
    if count is None:
        return 0.0
    return FORMATION_DEFENDER_BONUS_TIERS.get(count, 0.0)


def _find_real_player(real_match: dict, player_id: str) -> dict | None:
    """Cherche `player_id` dans /championship-match/{matchId} (home ou away).
    Tant que le vrai match n'a pas donne son coup d'envoi, home/away se
    reduisent a {clubId, rank} (pas de cle "players" du tout) -- une journee
    MPG peut regrouper des vrais matchs a des heures differentes (observe
    Liga_Tapas J1, 2026-08-15 : fenetre MPG ouverte a 17:30 alors que certains
    matchs de Liga demarrent plus tard). Pas encore joue = pas encore de note,
    donc None comme pour un joueur introuvable."""
    for side in ("home", "away"):
        rp = real_match[side].get("players", {}).get(player_id)
        if rp:
            return rp
    return None


def _goal_times_for(real_match: dict, player_id: str) -> list[str]:
    """Minutes des buts REELS PERSONNELS de `player_id` dans `real_match` --
    evenements home["goals"]/away["goals"] (liste {scorerId, time, type,
    ...}), plus fiable que de juste compter stats.goals (donne aussi la
    minute pour l'affichage). Exclut type=="own" (bug corrige 2026-08-16,
    retour utilisateur -- Ismael Doukoure marque un CSC, scorerId POINTE bien
    vers lui, mais ce n'est PAS un vrai but a son actif -- cf.
    _own_goals_scored_by_player pour le traitement cote equipe qui l'encaisse)."""
    return [
        event.get("time", "")
        for side in ("home", "away")
        for event in (real_match[side].get("goals") or [])
        if event.get("scorerId") == player_id and event.get("type") != "own"
    ]


def _own_goals_scored_by_player(real_match: dict, own_side: str | None, player_id: str) -> list[dict]:
    """Buts CSC (type == "own") marques PAR CE JOUEUR precis dans SON PROPRE
    vrai match -- confirme sur donnees reelles (retour utilisateur 2026-08-16,
    Ligue_Camembert saison_021 div05 J14) : un CSC apparait dans
    real_match[COTE OPPOSE a `own_side`]["goals"] (jamais le cote du buteur
    lui-meme), avec scorerId = playerId du buteur -- exactement comme un but
    normal marque "pour" le cote oppose, sauf que le scorerId appartient au
    cote qui l'encaisse.

    Filtre sur `scorerId == player_id` (retour utilisateur 2026-08-23,
    correctif majeur -- l'ancienne version, _own_goal_events_for_side,
    credit­ait le CSC a TOUT joueur fantasy dont le PROPRE club reel avait
    beneficie de l'evenement QUELQUE PART dans le vrai match, sans jamais
    verifier QUI l'avait marque : verifie faux sur Ligue_2_EKT D18 J3
    QLRBXDCX2_21_18_3_1_2_3 -- Saidou Sow (buteur du CSC, ni chez user_402657
    ni chez user_367647) faisait gagner un CSC aux DEUX cotes simplement
    parce que Raphael Lipinski (home) ET Mathis Touho/Evan's Jean Lambert
    (away) partagent tous les trois le club reel beneficiaire, sans qu'aucun
    d'eux n'ait quoi que ce soit a voir avec le but lui-meme. Regle officielle
    MPG (confirmee par l'utilisateur) : un CSC ne compte dans CE match fantasy
    QUE si le BUTEUR est lui-meme aligne dans l'une des deux equipes qui
    s'affrontent -- il penalise alors SA PROPRE equipe fantasy (beneficie a
    l'adversaire), jamais une equipe tierce qui partagerait juste le meme
    club reel beneficiaire. `own_side` : player_slot.get("side") du joueur
    MPG concerne (son propre cote REEL, deja expose par get_division_matches)
    -- None/absent -> []."""
    if not own_side:
        return []
    opposite = "away" if own_side == "home" else "home"
    return [
        e for e in (real_match.get(opposite, {}).get("goals") or [])
        if e.get("type") == "own" and e.get("scorerId") == player_id
    ]


def compute_player_live_score(player_slot: dict, real_match: dict | None, is_captain: bool, captain_bonus: float, composition=None) -> dict:
    """`player_slot` : players[playerId] cote get_division_matches (position,
    firstName/lastName, matchId...). `real_match` : reponse
    get_championship_match(player_slot['matchId']), ou None si pas encore
    disponible/pas trouvee. `composition` : team["composition"] (formation),
    pour le bonus defenseurs (cf. docstring module)."""
    real_player = _find_real_player(real_match, player_slot["playerId"]) if real_match else None

    note = real_player.get("mpgRating") if real_player else None
    stats = (real_player.get("stats") or {}) if real_player else {}
    minutes = stats.get("minutes_played", 0)
    real_goals = stats.get("goals", 0)
    real_goal_times = _goal_times_for(real_match, player_slot["playerId"]) if real_match else []
    match_finished = bool(real_match) and real_match.get("period") in FINISHED_MATCH_PERIODS
    # "period" absent/None OU explicitement "preMatch" (cf.
    # NOT_STARTED_MATCH_PERIODS, retour utilisateur 2026-08-23) tant que le
    # vrai match n'a pas donne son coup d'envoi -- present (quelle que soit
    # la valeur : firstHalf, halfTime... jusqu'a fullTime) des qu'il a
    # demarre. Sert au Grotaldo provisoire (cf. docstring module, retour
    # utilisateur 2026-08-16) : un titulaire a 0 minute ne compte comme
    # "absent" que si SON PROPRE match a au moins commence, jamais avant
    # (meme garde-fou que match_finished pour les scores delirants).
    real_match_started = (
        bool(real_match)
        and real_match.get("period") is not None
        and real_match.get("period") not in NOT_STARTED_MATCH_PERIODS
    )
    # Note PAR DEFAUT (retour utilisateur 2026-08-17, "Pour jouer le cote
    # simulation a fond on va leur mettre une note de 5... meme comportement
    # que pour les matchs reportes") : des que le VRAI match du joueur n'a pas
    # encore demarre (real_match_started=False -- couvre aussi bien "pas
    # encore commence aujourd'hui" qu'un vrai report, indistinguables cote
    # get_championship_match, cf. Buonanotte/Toni Fernandez sur
    # QLKEN7K41_22_7_1_1_0_4 -- les deux ont "period": None), note/minutes
    # sont simules a 5/90 (cumulable avec bonus formation/capitaine/Zahia
    # comme un titulaire normal). NE s'applique PAS une fois le match
    # REELEMENT demarre (real_match_started=True) meme sans note/minutes
    # encore confirmes -- ce cas reste "en attente" (0 minute peut encore
    # devenir une vraie absence UNE FOIS le match termine, cf. match_finished
    # plus bas -- l'ecraser avec une note fictive casserait le Grotaldo
    # provisoire et la recherche du remplacant obligatoire, qui ont besoin de
    # la VRAIE valeur de minutes_played une fois le match en cours/termine).
    is_default_rating = False
    if note is None and not real_match_started:
        note = 5
        minutes = 90
        is_default_rating = True
    # Date du coup d'envoi du VRAI match de ce joueur (ex. "2026-08-16T...")
    # -- une journee MPG etale ses vrais matchs sur plusieurs jours (cf.
    # docstring module), donc une simple minute ("55'") ne suffit pas a
    # savoir DE QUEL JOUR elle parle (retour utilisateur 2026-08-16).
    # Exposee pour l'affichage cote UI dans goal_events (cf. apply_line_battles),
    # pas pour un calcul quelconque ici.
    real_match_date = real_match.get("date") if real_match else None
    # CSC reels marques PAR CE JOUEUR precis (cf. _own_goals_scored_by_player,
    # correctif 2026-08-23 -- NE PAS confondre avec "mon cote a beneficie
    # d'un CSC quelque part", l'ancien comportement fautif) -- compte contre
    # SA PROPRE equipe fantasy, jamais celle d'un tiers qui partagerait juste
    # le meme club reel. Deduplique au niveau equipe dans apply_line_battles
    # (peu probable qu'un joueur en marque 2, mais garde la meme forme de
    # liste par coherence). Enrichi de la date ici (le meme real_match_date
    # que ci-dessus -- l'evenement brut MPG n'a qu'un timestamp epoch, pas une
    # date lisible directement).
    real_own_goal_scored_events = [
        {**e, "date": real_match_date}
        for e in _own_goals_scored_by_player(real_match, player_slot.get("side"), player_slot["playerId"])
    ] if real_match else []

    position = player_slot.get("position")
    bonus_formation = formation_defender_bonus(position, composition)
    bonus_cap = captain_bonus if is_captain else 0.0
    note_finale = (note + bonus_formation + bonus_cap) if note is not None else None

    return {
        "name": f"{player_slot.get('firstName', '')} {player_slot.get('lastName', '')}".strip(),
        "position": position,
        "real_goals": real_goals,
        "real_goal_times": real_goal_times,
        "real_match_date": real_match_date,
        "real_own_goal_scored_events": real_own_goal_scored_events,
        "note": note,
        "is_default_rating": is_default_rating,
        "minutes_played": minutes,
        "match_finished": match_finished,
        "real_match_started": real_match_started,
        "bonus_formation_defenseurs": bonus_formation,
        "bonus_capitaine": bonus_cap,
        "note_finale": note_finale,
    }


def _is_played(player_computed: dict) -> bool:
    return (player_computed.get("minutes_played") or 0) > 0


def _is_confirmed_played(player_computed: dict) -> bool:
    """Comme _is_played, mais EXCLUT les notes par defaut (retour utilisateur
    2026-08-17) -- reserve a la passe 2 (remplacement OBLIGATOIRE, recherche
    sur le banc, cf. plus bas) : un candidat trouve par CETTE recherche doit
    avoir REELLEMENT joue, une note par defaut (vrai match pas encore
    demarre/reporte, cf. compute_player_live_score) ne prouve rien. PAS utilisee
    pour la passe 1 (tactique, cf. plus bas -- la, une note par defaut suffit,
    c'est un choix DEJA DESIGNE par le manager, pas une recherche de notre
    cru) ni pour "ce titulaire compte-t-il comme aligne" (_is_played suffit
    aussi, cf. les usages plus bas). Sans cette distinction, un remplacant
    "obligatoire" sur le BANC dont le vrai match est LUI AUSSI pas encore
    joue/reporte etait considere "a bien joue" (minutes=90 factices) et
    entrait au jeu -- observe : plusieurs remplacants a note exactement
    5/5.5 (la valeur par defaut) recevant meme un bonus Zahia, alors qu'ils
    n'avaient en realite rien joue non plus."""
    return _is_played(player_computed) and not player_computed.get("is_default_rating")


def _note_with_simulated_bonuses(
    note: float | None, position: int | None,
    own_bonus: str | None, is_target: bool, incoming_bonus: str | None = None,
) -> float | None:
    """Note d'UN titulaire APRES tous les bonus de note simules -- SES PROPRES
    coups (own_bonus/target_player_id) ET ceux SUBIS de l'adversaire
    (incoming_bonus, retour utilisateur 2026-08-23 : "verifie que ce ne soit
    pas le cas pour tous les bonus", suite du correctif McDo+/Zahia).
    Purement informatif/pour comparaison, ne mute RIEN (l'application reelle
    sur le XI final reste le role unique de apply_own_bonus_effect/
    apply_incoming_bonus_effect, cf. leurs docstrings) -- les deux cumulables
    (un manager peut simuler simultanement SON bonus et celui de l'adversaire,
    cf. /api/live-scenario, home_choice ET opponent_choice).

    - boostOnePlayer (McDo+, own) : +1, seulement sur `is_target`.
    - boostAllPlayers (Zahia, own) : +0.5, tous les titulaires DECLARES --
      meme ceux ensuite remplaces (seuls leurs REMPLACANTS en sont exclus,
      cf. apply_own_bonus_effect) -- donc sans filtre `is_target` ici.
    - nerfGoalkeeper (Suarez, incoming/adverse) : -1, SEULEMENT le gardien.
    - nerfAllPlayers (Cheat Code, incoming/adverse) : -0.5, tous les joueurs
      DE CHAMP (jamais le gardien).

    blockTacticalSubs (Tonton Pat') n'affecte AUCUNE note (desactive toute la
    passe 1, deja gere via le parametre block_tactical de
    resolve_starting_lineup) et removeGoal (Valise) agit sur le SCORE, pas
    une note de joueur (apply_removed_goal, apres le duel de lignes) -- ni
    l'un ni l'autre ne concerne cette fonction. None si `note` est None (pas
    encore de note du tout, distinct de la note par defaut 5)."""
    if note is None:
        return None
    result = note
    if own_bonus == "boostOnePlayer" and is_target:
        result += 1
    elif own_bonus == "boostAllPlayers":
        result += 0.5
    if incoming_bonus == "nerfGoalkeeper" and position == POSITION_GOALKEEPER:
        result -= 1
    elif incoming_bonus == "nerfAllPlayers" and position != POSITION_GOALKEEPER:
        result -= 0.5
    return round(result, 2)


def resolve_starting_lineup(
    team: dict, real_matches_by_id: dict[str, dict], block_tactical: bool = False,
    own_bonus: str | None = None, target_player_id: str | None = None, incoming_bonus: str | None = None,
) -> dict:
    """Resout les remplacements (tactiques puis obligatoires, cf. docstring
    module) pour les 11 titulaires de `team`. `block_tactical` : Tonton Pat'
    joue par l'ADVERSAIRE (ou renvoye par un Miroir, cf. docstring module) --
    saute la passe 1 (tactique) pour les slots pas deja resolus par MPG, la
    passe 2 (obligatoire) reste active. `own_bonus`/`target_player_id`/
    `incoming_bonus` : bonus de note simules -- McDo+ (boostOnePlayer, cible
    target_player_id) ou Zahia (boostAllPlayers, tous les titulaires
    DECLARES) cotes SOI ; Suarez (nerfGoalkeeper, gardien seulement) ou Cheat
    Code (nerfAllPlayers, joueurs de champ) SUBIS de l'adversaire (retour
    utilisateur 2026-08-23, "verifie que ce ne soit pas le cas pour tous les
    bonus") -- en vrai MPG, le seuil de remplacement tactique compare la note
    APRES bonus (siens ET subis), pas avant, sinon la simulation "et si
    j'avais mis/subi ce bonus" ne peut jamais changer un remplacement (verifie
    McDo+/Zahia : note par defaut 5, seuil <6, +1/+0.5 doit pouvoir l'annuler
    -- symetriquement, un nerfAllPlayers adverse doit pouvoir en DECLENCHER un
    qui n'aurait pas eu lieu sinon). Cf. _note_with_simulated_bonuses. Cette
    note boostee/nerfee n'est utilisee ICI que pour la comparaison de seuil de
    la passe 1 ci-dessous -- elle n'est PAS appliquee en dur a
    computed[...]["note_finale"] (qui reste la note nue) : l'application
    definitive + le tag d'affichage sur le XI FINAL restent le role unique de
    apply_own_bonus_effect/apply_incoming_bonus_effect (appeles par
    compute_team_live_score), pour ne jamais compter un bonus deux fois.
    blockTacticalSubs/removeGoal n'ont pas leur place ici (aucun effet de
    note, cf. _note_with_simulated_bonuses) -- le premier est deja gere via
    `block_tactical` ci-dessus, le second agit sur le score apres coup
    (apply_removed_goal). Renvoie {players, rotaldo_count,
    csc_conceded, bench, out} -- `players` : {playerId_final: {...compute_player_live_score,
    "effective_note", "is_rotaldo", "sub_source", "poste_sautes",
    "replaced_starter"}}, cle par le joueur qui joue REELLEMENT le slot (peut
    differer du titulaire d'origine). `bench` : liste des joueurs de banc
    (slots > 11, cf. bench_ids) dans l'ordre du banc, qu'ils soient entres ou
    non -- absents de `players` sinon (seul le titulaire EFFECTIF y figure).
    `out` : liste des titulaires D'ORIGINE reellement sortis (substitution
    tactique ou obligatoire effective, cf. "Joueurs sortis" -- retour
    utilisateur 2026-08-10), avec LEUR PROPRE note/minutes (jamais celles du
    remplacant) -- exclut le cas Rotaldo "personne trouve" (le titulaire n'est
    pas sorti, juste note Rotaldo faute de remplacant valide).

    IMPORTANT (observe 2026-08-10) : une fois un remplacement tactique
    reellement declenche EN DIRECT, MPG met a jour playersOnPitch lui-meme --
    le slot 1-11 montre alors directement le remplacant, et le titulaire
    d'origine DISPARAIT de playersOnPitch (mais reste dans team["players"],
    avec son propre matchId, calculable comme n'importe quel joueur). On ne
    peut donc pas supposer que playersOnPitch[1..11] contient toujours les
    titulaires D'ORIGINE -- il faut croiser avec team["tacticalSubs"] pour
    retrouver le vrai titulaire nominal d'un slot deja resolu par MPG, auquel
    cas on FAIT CONFIANCE a la decision de MPG (pas de re-comparaison de note
    cote nous, qui de toute facon n'aurait plus le titulaire pour comparer)."""
    all_slots = team["playersOnPitch"]
    players_raw = team["players"]
    captain_id = team.get("captain")
    captain_bonus = (team.get("bonuses") or {}).get("captain", {}).get("bonusRating", 0.5)

    composition = team.get("composition")
    computed: dict[str, dict] = {}
    for slot in all_slots.values():
        pid = slot["playerId"]
        player_slot = players_raw.get(pid, {})
        real_match = real_matches_by_id.get(player_slot.get("matchId"))
        computed[pid] = compute_player_live_score(player_slot, real_match, pid == captain_id, captain_bonus, composition)

    xi_slot_ids = [all_slots[str(i)]["playerId"] for i in range(1, 12) if str(i) in all_slots]
    xi_slot_id_set = set(xi_slot_ids)
    bench_slot_keys = sorted((k for k in all_slots if k.isdigit() and int(k) > 11), key=int)
    # L'API MPG duplique parfois un titulaire/remplacant deja promu (slot 1-11)
    # sur un slot de banc precoce (ex. slot 11 ET 12 pointent le meme playerId,
    # observe 2026-08-10) -- l'exclure du banc : il joue deja sa propre place,
    # le reutiliser comme "remplacant disponible" pour un AUTRE slot serait un
    # vrai bug (meme joueur represente deux slots), pas juste un doublon
    # d'affichage.
    bench_ids = [pid for pid in (all_slots[k]["playerId"] for k in bench_slot_keys) if pid not in xi_slot_id_set]

    tactical_subs = {ts["starterId"]: ts for ts in (team.get("tacticalSubs") or [])}
    sub_to_starter = {ts["subId"]: ts["starterId"] for ts in (team.get("tacticalSubs") or [])}

    # Detecte les slots DEJA resolus par MPG en direct (cf. docstring
    # "IMPORTANT" ci-dessus) : le slot montre le remplacant, dont le titulaire
    # nominal (sub_to_starter) est absent de playersOnPitch -- on calcule alors
    # sa propre note (matchId dans team["players"]) pour "out"/l'affichage.
    slot_to_nominal: dict[str, str] = {}
    for pid in xi_slot_ids:
        starter_id = sub_to_starter.get(pid)
        if starter_id and starter_id not in xi_slot_id_set:
            if starter_id not in computed:
                starter_slot = players_raw.get(starter_id)
                if starter_slot:
                    real_match = real_matches_by_id.get(starter_slot.get("matchId"))
                    computed[starter_id] = compute_player_live_score(starter_slot, real_match, starter_id == captain_id, captain_bonus, composition)
            slot_to_nominal[pid] = starter_id if starter_id in computed else pid
        else:
            slot_to_nominal[pid] = pid

    xi_ids = [slot_to_nominal[pid] for pid in xi_slot_ids]  # titulaires NOMINAUX (jamais un remplacant deja promu)
    used_ids: set[str] = set()
    resolution: dict[str, dict] = {}
    for slot_pid, starter_id in zip(xi_slot_ids, xi_ids):
        if slot_pid != starter_id:
            used_ids.add(slot_pid)
            resolution[starter_id] = {
                "playerId": slot_pid, "source": "tactique", "poste_sautes": 0,
                "rating": (tactical_subs.get(starter_id) or {}).get("rating"),
            }

    # Passe 1 : remplacements tactiques (prioritaires), jamais pour un gardien.
    # Si le remplacant designe n'a pas joue, il n'y a personne a faire entrer --
    # le remplacement tactique ne se fait PAS (le titulaire reste, cf. retour
    # utilisateur 2026-08-08 -- le risque du tactique, c'est une note moins
    # bonne que prevu chez le remplacant, PAS son absence de jeu). Ignore les
    # slots deja resolus par MPG ci-dessus (resolution deja renseignee), et la
    # passe entiere si Tonton Pat' bloque (block_tactical, cf. docstring).
    #
    # IMPORTANT (retour utilisateur 2026-08-17) : ici, une note PAR DEFAUT
    # suffit (_is_played, PAS _is_confirmed_played) -- contrairement a la
    # passe 2 ci-dessous. Le remplacement tactique est un choix DEJA DESIGNE
    # par le manager (team["tacticalSubs"], pas une recherche de notre cru) :
    # une fois le seuil de note franchi, MPG le declenche avec la note qu'a
    # le remplacant a cet instant, meme provisoire -- verifie exact sur
    # Ligue_2_EKT/QLKEN7K41_22_7_1_1_0_4 : Jose Angel Carmona absent, son
    # remplacant designe Denzel Dumfries (note par defaut 5, seuil 5.5)
    # doit rentrer en PRIORITE sur la recherche "obligatoire" (qui trouvait
    # a tort Omar El Hilali, un tout autre joueur, en ignorant le
    # remplacement tactique deja designe).
    #
    # IMPORTANT (retour utilisateur 2026-08-17, "tu fais remplacer Facundo
    # Buononotte qui n'a pas encore joue (mais le match n'est pas reporte)") :
    # note_finale a None ne veut PAS dire que le titulaire n'a pas joue -- si
    # SON PROPRE vrai match n'a pas encore demarre/pas encore fini, c'est juste
    # "pas encore de donnees" (meme garde-fou que match_finished en passe 2 /
    # docstring "IMPORTANT" plus haut). Sans ce garde-fou, un titulaire dont le
    # match n'a meme pas commence declenchait le remplacement tactique des le
    # premier poll, des qu'un remplacant designe avait deja une note (reelle ou
    # par defaut) -- verifie sur Ligue_2_EKT/QLKEN7K41_22_7_1_1_0_4 : Buonanotte
    # ("hasMatchPostponed": False, aucune note) remplace a tort par Toni
    # Fernandez (son propre match, lui, genuinement reporte). On ne substitue
    # que si (a) une note existe et est sous le seuil, ou (b) aucune note mais
    # le vrai match du titulaire est TERMINE (absence confirmee) -- sinon on
    # laisse la passe 2 le classer "en_attente" comme n'importe quel titulaire
    # dont le sort n'est pas encore tranche.
    for starter_id in xi_ids:
        if starter_id in resolution or block_tactical:
            continue
        starter = computed.get(starter_id)
        if not starter or starter["position"] == POSITION_GOALKEEPER:
            continue
        ts = tactical_subs.get(starter_id)
        if not ts or ts["subId"] in used_ids or ts["subId"] not in computed:
            continue
        sub_player = computed[ts["subId"]]
        if not _is_played(sub_player):
            continue
        note = starter["note_finale"]
        # Bonus de note simules -- siens (McDo+/Zahia) ET subis de l'adversaire
        # (Suarez/Cheat Code, cf. _note_with_simulated_bonuses et la docstring
        # ci-dessus) : seule la comparaison au seuil en tient compte, jamais
        # computed[...]["note_finale"] lui-meme. Gate sur is_default_rating
        # (retour utilisateur 2026-08-23) : le choix de bonus est verrouille
        # AVANT le coup d'envoi -- une fois le VRAI match de ce titulaire
        # demarre/termine, sa note est reelle et confirmee, plus rien a
        # "simuler" (le remplacement, s'il a eu lieu, s'est deja reellement
        # produit). Le bonus ne peut donc influencer le seuil que tant que ce
        # titulaire n'a pas encore reellement joue (note par defaut 5, cf.
        # compute_player_live_score).
        if starter.get("is_default_rating"):
            boosted = _note_with_simulated_bonuses(
                note, starter["position"], own_bonus, starter_id == target_player_id, incoming_bonus,
            )
            if boosted is not None:
                note = boosted
        should_sub = (note is not None and note < ts["rating"]) or (note is None and starter["match_finished"])
        if not should_sub:
            continue
        used_ids.add(ts["subId"])
        resolution[starter_id] = {"playerId": ts["subId"], "source": "tactique", "poste_sautes": 0, "rating": ts["rating"]}

    # Passe 2 : remplacement obligatoire -- seulement titulaires non couverts
    # par le tactique et n'ayant pas joue. Cascade poste identique -> poste(s)
    # inferieur(s), jamais au-dela de DF (cf. hypothese non confirmee, docstring).
    for starter_id in xi_ids:
        if starter_id in resolution:
            continue
        starter = computed.get(starter_id)
        if starter and _is_played(starter):
            resolution[starter_id] = {"playerId": starter_id, "source": "titulaire", "poste_sautes": 0}
            continue
        if not starter:
            resolution[starter_id] = {"playerId": None, "source": "rotaldo", "poste_sautes": 0}
            continue
        if not starter["match_finished"]:
            # Vrai match pas encore termine (pas commence ou en cours) -- 0
            # minute ne prouve pas une absence, cf. docstring "IMPORTANT"
            # ci-dessus. On ne substitue pas, on ne Rotaldo pas : en attente.
            resolution[starter_id] = {"playerId": starter_id, "source": "en_attente", "poste_sautes": 0}
            continue

        position = starter["position"]
        candidate_positions = [position] if position == POSITION_GOALKEEPER else list(range(position, POSITION_DEFENDER - 1, -1))
        found = None
        for poste_sautes, cand_position in enumerate(candidate_positions):
            for bench_id in bench_ids:
                if bench_id in used_ids:
                    continue
                cand = computed.get(bench_id)
                if cand and cand["position"] == cand_position and _is_confirmed_played(cand):
                    found = (bench_id, poste_sautes)
                    break
            if found:
                break

        if found:
            bench_id, poste_sautes = found
            used_ids.add(bench_id)
            resolution[starter_id] = {"playerId": bench_id, "source": "obligatoire", "poste_sautes": poste_sautes}
        else:
            resolution[starter_id] = {"playerId": None, "source": "rotaldo", "poste_sautes": 0}

    players_out: dict[str, dict] = {}
    starters_out: list[dict] = []
    rotaldo_count = 0
    for starter_id in xi_ids:
        res = resolution[starter_id]
        starter_name = computed.get(starter_id, {}).get("name")

        if res["source"] == "rotaldo":
            rotaldo_count += 1
            base = computed.get(starter_id) or {"name": starter_name or "?", "position": None}
            players_out[starter_id] = {
                **base, "effective_note": ROTALDO_NOTE, "is_rotaldo": True,
                "sub_source": "rotaldo", "poste_sautes": 0, "replaced_starter": None,
            }
            continue

        if res["source"] == "en_attente":
            # Vrai match pas termine, cf. passe 2 -- pas de note, pas de
            # Rotaldo, exclu du total/duel de lignes tant que ca dure.
            players_out[starter_id] = {
                **computed[starter_id], "effective_note": None, "is_rotaldo": False,
                "sub_source": "en_attente", "poste_sautes": 0, "replaced_starter": None,
            }
            continue

        final_id = res["playerId"]
        final_player = computed[final_id]
        penalty = float(res["poste_sautes"])
        is_rotaldo = final_player["note_finale"] is None
        effective_note = ROTALDO_NOTE if is_rotaldo else round(final_player["note_finale"] - penalty, 2)
        # Bonus formation (X defenseurs) RETIRE pour un remplacant (tactique
        # OU obligatoire, final_id != starter_id) -- retour utilisateur
        # 2026-08-17 : "Denzel Dumfries arrive en remplacant tactique MAIS il
        # beneficie du bonus (4 defenseurs) ce qui est impossible". Meme
        # logique que Zahia (cf. apply_own_bonus_effect) et le capitaine (deja
        # correct par construction, lie a un ID de joueur precis, jamais celui
        # du remplacant) : un bonus "declaratif" tenant a la composition/au
        # XI ANNONCE ne suit pas le remplacant sur le terrain, meme si celui-ci
        # est lui-meme defenseur dans l'absolu -- il etait deja baked dans
        # final_player["note_finale"] (cf. compute_player_live_score), donc on
        # le retranche ici plutot que de re-derailler tout le calcul du seuil
        # de remplacement tactique (base sur note_finale AVANT ce retrait).
        if not is_rotaldo and final_id != starter_id:
            effective_note = round(effective_note - final_player["bonus_formation_defenseurs"], 2)
        if is_rotaldo:
            rotaldo_count += 1

        players_out[final_id] = {
            **final_player, "effective_note": effective_note, "is_rotaldo": is_rotaldo,
            "sub_source": res["source"], "poste_sautes": res["poste_sautes"],
            "replaced_starter": starter_name if final_id != starter_id else None,
        }

        # "Joueurs sortis" : le titulaire D'ORIGINE quand une substitution a
        # reellement eu lieu (final_id != starter_id) -- pas le cas Rotaldo
        # "personne trouve" ci-dessus, ou l'id du titulaire reste la cle (il
        # n'est pas "sorti", juste note Rotaldo faute de remplacant valide).
        # Expose SA PROPRE note (computed[starter_id], jamais celle du
        # remplacant) pour lever l'ambiguite cote UI (retour utilisateur
        # 2026-08-10).
        if final_id != starter_id:
            starter_info = computed[starter_id]
            is_target = starter_id == target_player_id
            note_avec_bonus = _note_with_simulated_bonuses(
                starter_info["note_finale"], starter_info["position"], own_bonus, is_target, incoming_bonus,
            )
            starters_out.append({
                "playerId": starter_id, "name": starter_info["name"], "position": starter_info["position"],
                "note_finale": starter_info["note_finale"], "minutes_played": starter_info["minutes_played"],
                "reason": res["source"],  # "tactique" ou "obligatoire"
                "replaced_by": final_player["name"],
                # True si CE titulaire est sorti sur une note PAR DEFAUT (son
                # propre vrai match n'a pas encore demarre, cf.
                # compute_player_live_score) -- distingue un remplacement
                # encore PROVISOIRE (une simulation de bonus peut encore
                # changer l'issue, cf. own_bonus ci-dessus) d'un remplacement
                # REEL deja confirme (vrai match joue/termine -- rien a
                # simuler, retour utilisateur 2026-08-23 : les managers ne
                # doivent pas croire pouvoir booster un joueur qui a deja
                # joue).
                "is_default_rating": starter_info["is_default_rating"],
                # Retour utilisateur 2026-08-23 (2e/3e passage, Zahia puis
                # generalisation a tous les bonus de note) : le manager doit
                # voir DEUX notes distinctes pour un titulaire sorti -- sa
                # note AVEC le(s) bonus simule(s) (purement informatif, cf.
                # _note_with_simulated_bonuses) et celle qui a REELLEMENT
                # determine son remplacement (identique a note_avec_bonus
                # seulement si is_default_rating -- cf. la passe 1 ci-dessus,
                # gate sur is_default_rating -- sinon identique a note_finale,
                # le bonus n'a jamais pu influencer une decision deja reelle).
                "note_avec_bonus": note_avec_bonus,
                "note_seuil": (
                    note_avec_bonus if (res["source"] == "tactique" and starter_info["is_default_rating"]
                                         and note_avec_bonus is not None)
                    else starter_info["note_finale"]
                ),
                # Seuil CONFIGURE par le manager pour ce remplacement tactique
                # (team["tacticalSubs"][...]["rating"], ex. 6 pour Manzambi ->
                # Foden) -- retour utilisateur 2026-08-25, "je veux que cette
                # note soit affichee ... pour que les managers verifient la
                # coherence des choix effectues". A NE PAS CONFONDRE avec
                # note_seuil ci-dessus (qui est la note COMPAREE au seuil, pas
                # le seuil lui-meme) -- ce champ-ci est LE NOMBRE FIXE que le
                # manager a configure avant le match, jamais mute par un bonus
                # simule. None pour un remplacement "obligatoire" (aucun seuil,
                # cf. passe 2 -- declenche par une absence de note, pas une
                # comparaison).
                "seuil_configure": res.get("rating"),
            })

    bench = []
    for pid in bench_ids:
        info = computed.get(pid)
        if not info:
            continue
        # Bonus formation (X defenseurs) RETIRE ici aussi (meme raison que
        # dans players_out ci-dessus, cf. commit Dumfries) : bonus_formation_
        # defenseurs est calcule pour TOUT joueur des sa propre position,
        # meme sur le banc -- un remplacant qui n'est meme pas ENTRE ne
        # devrait a fortiori pas en beneficier a l'affichage (retour
        # utilisateur 2026-08-17, Aymeric Laporte affiche a 5.5 sur le banc :
        # 5 par defaut, note propre + 0.5 de formation qu'il n'a jamais
        # gagne, jamais entre sur le terrain).
        bench_note = info["note_finale"]
        if bench_note is not None:
            bench_note = round(bench_note - info["bonus_formation_defenseurs"], 2)
        bench.append({
            "playerId": pid, "name": info["name"], "position": info["position"],
            "note_finale": bench_note, "minutes_played": info["minutes_played"],
            "entered": pid in players_out,
        })

    return {
        "players": players_out, "rotaldo_count": rotaldo_count, "csc_conceded": rotaldo_count // 3,
        "bench": bench, "out": starters_out,
    }


VALID_BONUSES = {
    "boostAllPlayers",    # Zahia
    "boostOnePlayer",     # McDo+
    "nerfGoalkeeper",     # Suarez
    "nerfAllPlayers",     # Cheat Code 18-26
    "blockTacticalSubs",  # Tonton Pat'
    "removeGoal",         # Valise a Nanard
    "mirror",             # Miroir
}

# Taille de division MINIMALE a partir de laquelle chaque bonus existe (tableau
# officiel MPG "Nombre de bonus par ligue", retour utilisateur 2026-08-11 --
# colonnes 2/4/6/8/10, "-" = pas encore alloue). Pas de suivi du nombre DEJA
# utilise (aucun stockage d'usage, cf. docstring module "Coups") -- seulement
# la DISPONIBILITE du type pour cette taille, comparee via >= (couvre les
# tailles intermediaires/superieures non listees telles quelles, ex. 7 ou 12).
BONUS_MIN_DIVISION_SIZE = {
    "removeGoal": 4,          # Valise a Nanard
    "boostOnePlayer": 4,      # McDo+
    "nerfGoalkeeper": 6,      # Suarez
    "boostAllPlayers": 6,     # Zahia
    "mirror": 6,              # Miroir
    "fourStrikers": 6,        # 424 -- retour utilisateur 2026-08-18 (audit
                               # collaborateur, B6) : MPG en a 8, cette table
                               # en oubliait un. Disponibilite seulement --
                               # PAS dans VALID_BONUSES, deja gere ailleurs
                               # (core/scoring.py::BONUS_LABELS,
                               # core/bonus_impact.py) sans effet de note
                               # simule ici (aucune preuve qu'il en ait un,
                               # contrairement aux 7 "coups" ci-dessus --
                               # a confirmer avant de l'ajouter a
                               # apply_own_bonus_effect/VALID_BONUSES).
    "nerfAllPlayers": 8,      # Cheat Code 18-26
    "blockTacticalSubs": 8,   # Tonton Pat'
}


def bonus_available_for_division_size(bonus: str, division_size: int) -> bool:
    return division_size >= BONUS_MIN_DIVISION_SIZE.get(bonus, 0)

# Categorie d'un coup pour le Miroir (cf. docstring module) -- "self" =
# beneficie a son propre lanceur (VOLE par le Miroir adverse), "opponent" =
# nuit a l'ADVERSAIRE du lanceur (RENVOYE contre son lanceur par le Miroir).
# "mirror" lui-meme n'a pas de categorie (meta-effet, cf. resolve_match_bonus_effects).
SELF_BONUSES = {"boostAllPlayers", "boostOnePlayer"}
OPPONENT_BONUSES = {"nerfGoalkeeper", "nerfAllPlayers", "blockTacticalSubs", "removeGoal"}


def _bonus_category(bonus: str | None) -> str | None:
    if bonus in SELF_BONUSES:
        return "self"
    if bonus in OPPONENT_BONUSES:
        return "opponent"
    return None


# Coups qui laissent une trace exploitable sur la note d'un joueur via
# player["bonusesDetails"] (cf. detect_confirmed_bonus_choices) -- removeGoal/
# mirror/blockTacticalSubs n'affectent pas une note directement (annulation de
# but, redirection, blocage de sub), pas de trace ici, hors scope pour la
# reconstruction post-match (retour utilisateur 2026-08-11).
_NOTE_AFFECTING_BONUSES = {"boostAllPlayers", "boostOnePlayer", "nerfGoalkeeper", "nerfAllPlayers"}


def _detect_team_bonus(team_players_raw: dict) -> dict | None:
    """{"bonus", "targetPlayerId"} du premier coup a effet de note trouve dans
    bonusesDetails des PROPRES joueurs de cette equipe (peu importe qui l'a
    subi/en a beneficie -- cf. detect_confirmed_bonus_choices pour
    l'attribution au bon userId), ou None si aucun des 4 bonus suivis n'y
    figure. "boostDefense4"/"boostDefense5" (bonus de formation, deja calcule
    independamment via formation_defender_bonus) et tout autre champ inconnu
    sont ignores -- ne pas re-additionner ce qu'on calcule deja nous-memes."""
    for pid, p in (team_players_raw or {}).items():
        details = p.get("bonusesDetails") or {}
        for key in details:
            if key in _NOTE_AFFECTING_BONUSES:
                return {"bonus": key, "targetPlayerId": pid if key == "boostOnePlayer" else None}
    return None


def detect_confirmed_bonus_choices(division_matches: list[dict]) -> dict[str, dict]:
    """bonus_choices tel qu'attendu par compute_division_live_scores, reconstruit
    a partir des coups REELLEMENT confirmes par MPG (player["bonusesDetails"],
    revele par get_division_matches() UNE FOIS le match reel termine -- meme
    principe que la resolution des remplacements tactiques, cf.
    resolve_starting_lineup docstring : MPG cache cette info pendant le direct
    pour ne pas se trahir a l'adversaire, et ne la revele qu'apres coup).

    A N'APPELER QUE sur des division_matches refetches fraichement pour un
    match DEJA ARCHIVE (cf. core.live_projection.is_gameweek_archived cote
    appelant) -- pendant un match en cours, bonusesDetails est absent/vide
    cote MPG, donc cette fonction ne trouverait de toute facon rien, mais ce
    n'est pas cette fonction qui garantit la non-fuite en direct, c'est a
    l'appelant de ne l'invoquer QUE pour du deja-archive (cf. retour
    utilisateur du 2026-08-09 sur la fuite d'info entre managers).

    boostOnePlayer/boostAllPlayers sont trouves sur les PROPRES joueurs du
    lanceur (self -- beneficiaire = meme equipe) ; nerfGoalkeeper/
    nerfAllPlayers sont trouves sur les joueurs de la VICTIME (l'equipe qui
    subit la note en moins), donc attribues a l'equipe ADVERSE (le lanceur
    reel) -- cf. SELF_BONUSES/OPPONENT_BONUSES. Retour utilisateur
    2026-08-11 : "Sainte-Luce a 6 [...] il a été boosté par un McDo et
    devrait avoir 7" -- capture de bonusesDetails.boostOnePlayer confirmee
    sur /division-match/... une fois le match termine."""
    choices: dict[str, dict] = {}
    for div_match in division_matches:
        home, away = div_match["home"], div_match["away"]
        home_user_id, away_user_id = home.get("userId"), away.get("userId")

        home_found = _detect_team_bonus(home.get("players"))
        if home_found:
            caster = home_user_id if home_found["bonus"] in SELF_BONUSES else away_user_id
            if caster:
                choices[caster] = home_found

        away_found = _detect_team_bonus(away.get("players"))
        if away_found:
            caster = away_user_id if away_found["bonus"] in SELF_BONUSES else home_user_id
            if caster:
                choices[caster] = away_found
    return choices


def apply_own_bonus_effect(players: dict[str, dict], bonus: str | None, target_player_id: str | None = None) -> None:
    """Effet d'un coup declare par le manager sur SA PROPRE equipe (Zahia,
    McDo+) -- mute p["effective_note"] du XI final (cf. docstring module,
    "Coups"). Les coups a effet sur l'ADVERSAIRE (Suarez, Cheat Code) sont
    geres par apply_incoming_bonus_effect, cote adversaire. Tague aussi
    p["bonus_tag"] = cle du coup sur CHAQUE joueur touche -- l'effet doit se
    voir sur SA note individuelle (et donc sur la ligne DF/MF/FW dont il fait
    partie pour le duel de lignes), pas comme un delta abstrait sur le total
    de l'equipe (retour utilisateur 2026-08-09 : "le cumul de points MPG n'a
    pas vraiment de sens").

    boostAllPlayers (Zahia) EXCLUT les remplacants (p["replaced_starter"] non
    None, cf. resolve_starting_lineup -- tactique ou obligatoire) : retour
    utilisateur 2026-08-17, "Une zahia s'applique uniquement aux titulaires...
    C'est impossible [de la voir sur un remplacant]", observe a tort sur Toni
    Fernandez (remplacant obligatoire/par defaut de Facundo Buonanotte,
    Ligue_2_EKT/QLKEN7K41_22_7_1_1_0_4)."""
    if bonus == "boostAllPlayers":
        for p in players.values():
            if p.get("replaced_starter"):
                continue
            if p["effective_note"] is not None:
                p["effective_note"] = round(p["effective_note"] + 0.5, 2)
                p["bonus_tag"] = "boostAllPlayers"
    elif bonus == "boostOnePlayer" and target_player_id in players:
        p = players[target_player_id]
        if p["effective_note"] is not None:
            p["effective_note"] = round(p["effective_note"] + 1, 2)
            p["bonus_tag"] = "boostOnePlayer"


def apply_incoming_bonus_effect(players: dict[str, dict], bonus: str | None) -> None:
    """Effet SUBI par une equipe suite a un coup joue par l'ADVERSAIRE
    (Suarez, Cheat Code) -- cf. docstring module, "Coups". Tague p["bonus_tag"]
    comme apply_own_bonus_effect, cf. sa docstring."""
    if bonus == "nerfGoalkeeper":
        for p in players.values():
            if p["position"] == POSITION_GOALKEEPER and p["effective_note"] is not None:
                p["effective_note"] = round(p["effective_note"] - 1, 2)
                p["bonus_tag"] = "nerfGoalkeeper"
    elif bonus == "nerfAllPlayers":
        for p in players.values():
            if p["position"] != POSITION_GOALKEEPER and p["effective_note"] is not None:
                p["effective_note"] = round(p["effective_note"] - 0.5, 2)
                p["bonus_tag"] = "nerfAllPlayers"


def compute_team_live_score(
    team: dict, real_matches_by_id: dict[str, dict],
    own_bonus: dict | None = None, incoming_bonus: str | None = None, block_tactical: bool = False,
) -> dict:
    """`team` : team["home"] ou team["away"] de get_division_matches().
    `real_matches_by_id` : {matchId: get_championship_match(matchId)}, deja
    recuperees par l'appelant (mutualisees entre les 2 equipes du match / le
    reste de la division -- plusieurs joueurs partagent le meme matchId reel).
    Resout d'abord les remplacements (resolve_starting_lineup, avec
    block_tactical si Tonton Pat' cible cette equipe -- own_bonus/targetPlayerId
    ET incoming_bonus lui sont aussi transmis : tout bonus de note simule
    (McDo+/Zahia siens, Suarez/Cheat Code subis) doit pouvoir influencer la
    decision de remplacement tactique elle-meme, cf. sa docstring, retour
    utilisateur 2026-08-23), puis applique les coups de note declares
    (own_bonus = {"bonus", "targetPlayerId"} de CETTE equipe, incoming_bonus =
    cle du coup joue par l'ADVERSAIRE, cf. docstring module) -- "total" est la
    somme des effective_note APRES coups, PRE duel de lignes. removeGoal
    (Valise) n'est PAS applique ici -- il agit sur les buts, determines
    seulement apres apply_line_battles, cf. compute_division_live_scores."""
    lineup = resolve_starting_lineup(
        team, real_matches_by_id, block_tactical=block_tactical,
        own_bonus=(own_bonus or {}).get("bonus"), target_player_id=(own_bonus or {}).get("targetPlayerId"),
        incoming_bonus=incoming_bonus,
    )
    if own_bonus:
        apply_own_bonus_effect(lineup["players"], own_bonus.get("bonus"), own_bonus.get("targetPlayerId"))
    if incoming_bonus:
        apply_incoming_bonus_effect(lineup["players"], incoming_bonus)
    # "en_attente" (vrai match pas termine) laisse effective_note=None -- exclu
    # du total, qui reste donc PROVISOIRE (augmente au fil des vrais matchs
    # de la journee qui se terminent, cf. docstring module).
    total = sum(p["effective_note"] for p in lineup["players"].values() if p["effective_note"] is not None)

    return {
        "total": round(total, 2), "players": lineup["players"], "bench": lineup["bench"], "out": lineup["out"],
        "userId": team.get("userId"), "formation": team.get("composition"),
        "rotaldo_count": lineup["rotaldo_count"], "csc_conceded": lineup["csc_conceded"],
    }


def _line_average(players_by_id: dict[str, dict], position: int) -> float | None:
    """Moyenne des effective_note (post-remplacement, cf. resolve_starting_lineup)
    des joueurs d'un poste donne -- pour un gardien, il n'y en a qu'un, la
    "moyenne" est donc sa propre note."""
    notes = [p["effective_note"] for p in players_by_id.values() if p["position"] == position and p["effective_note"] is not None]
    return sum(notes) / len(notes) if notes else None


def _resolve_line_battle(note_finale: float, position: int, opponent_lines: dict[int, float | None], is_home: bool, real_goals: int = 0) -> dict:
    """Fait avancer UN joueur (note deja bonus compris) le long de sa sequence
    de lignes adverses (LINE_SEQUENCE_BY_POSITION), s'arrete a la premiere
    ligne non franchie. `opponent_lines` : {position -> moyenne}, fige avant
    tout duel (cf. docstring module).

    Regle officielle MPG R1 (retour utilisateur 2026-08-18, audit
    collaborateur, confirme avec Ilan) : il faut au moins 5/10 pour marquer
    un but MPG -- la note EFFECTIVE dans le XI final, bonus compris (donc
    `note_finale`/`effective_note` tel que recu ICI, AVANT toute penalite de
    duel -- pas note_apres_duel une fois les lignes franchies grignotees).
    Un defenseur a 4 qui passe a 5 grace a une defense a cinq peut donc
    marquer. Absent avant ce correctif -- verifie : un attaquant a 4,5 face
    a une defense de Rotaldos se voyait a tort accorder un but MPG."""
    sequence = LINE_SEQUENCE_BY_POSITION.get(position, [])
    current_note = note_finale
    lines_passed: list[int] = []

    for opponent_position in sequence:
        opponent_avg = opponent_lines.get(opponent_position)
        if opponent_avg is None:
            break  # ligne adverse vide (composition incomplete) -- on s'arrete la, prudent.

        if current_note > opponent_avg:
            passed = True
        elif current_note == opponent_avg:
            passed = is_home
        else:
            passed = False

        if not passed:
            break

        penalty = -1.0 if not lines_passed else -0.5
        current_note += penalty
        lines_passed.append(opponent_position)

    # Le joueur "passe" quand meme la ligne du gardien (penalite deja appliquee
    # ci-dessus) meme s'il a deja marque en reel -- seul le COMPTAGE du but MPG
    # est retire, pas le fait d'avoir gagne son duel (donc pas d'ajout a la note).
    reached_keeper = lines_passed and lines_passed[-1] == POSITION_GOALKEEPER
    goal_scored = reached_keeper and real_goals == 0 and note_finale >= 5

    return {
        "lines_passed": lines_passed,
        "but_mpg_bonus": bool(goal_scored),
        "note_apres_duel": round(current_note, 2),
    }


def apply_line_battles(match: dict) -> dict:
    """Ajoute le duel de lignes maison (cf. docstring module) a un resultat de
    compute_division_live_scores() -- {matchId, home, away}. Mute et renvoie
    `match` : ajoute lines_passed/but_mpg_bonus/note_apres_duel a chaque
    joueur de champ, laisse les gardiens inchanges. IMPORTANT : note_apres_duel
    sert UNIQUEMENT a determiner but_mpg_bonus (franchir toutes les lignes) --
    ni affiche ni comptabilise ailleurs. team["total"] reste celui pose par
    compute_team_live_score (somme des effective_note, note reelle
    post-remplacement mais PRE duel -- sinon les deux mecanismes se melangent,
    cf. retour utilisateur 2026-08-08). Ajoute aussi team["buts_mpg"]/
    ["real_goals"]/["score"] (real_goals PERSONNELS + CSC reels beneficiant a
    cette equipe + buts_mpg + CSC Rotaldo concedes par l'adverse),
    team["goal_events"] (liste {name, type: "real"|"mpg"|"csc"|"real_og", time,
    date} triable pour un affichage type feuille de match -- "real_og" = CSC
    reel marque PAR UN JOUEUR ALIGNE DANS LE XI FINAL DE L'EQUIPE ADVERSE de
    CE match fantasy (jamais un joueur tiers qui partagerait juste le meme
    club reel beneficiaire, cf. _own_goals_scored_by_player, correctif majeur
    2026-08-23 -- retour utilisateur : "le CSC ne peut etre compte que si le
    joueur l'ayant marque est un joueur de l'effectif... sous condition qu'il
    soit entre en jeu dans le match qui nous oppose"). "date" (ISO,
    ex. "2026-08-16T19:00:00.000Z") : uniquement pour "real"/"real_og"
    (evenements rattaches a un vrai match precis) -- None pour "mpg"/"csc"
    (pas de vrai match unique associe). Une
    journee MPG etale ses vrais matchs sur plusieurs jours, la seule minute
    ("55'") ne suffit pas a savoir de quel jour elle parle (retour
    utilisateur 2026-08-16) -- a afficher cote UI (JJ/MM) des que plusieurs
    dates distinctes apparaissent dans la meme feuille de match.) et
    team["line_averages"] (moyennes DF/MF/FW de SA PROPRE equipe -- cle =
    POSITION_DEFENDER/MIDFIELDER/FORWARD, deja calculees pour le duel de
    lignes, exposees ici pour l'affichage cote UI, cf. retour utilisateur
    2026-08-09)."""
    home_lines = {pos: _line_average(match["home"]["players"], pos) for pos in (POSITION_DEFENDER, POSITION_MIDFIELDER, POSITION_FORWARD, POSITION_GOALKEEPER)}
    away_lines = {pos: _line_average(match["away"]["players"], pos) for pos in (POSITION_DEFENDER, POSITION_MIDFIELDER, POSITION_FORWARD, POSITION_GOALKEEPER)}

    match["home"]["line_averages"] = {pos: home_lines[pos] for pos in (POSITION_DEFENDER, POSITION_MIDFIELDER, POSITION_FORWARD)}
    match["away"]["line_averages"] = {pos: away_lines[pos] for pos in (POSITION_DEFENDER, POSITION_MIDFIELDER, POSITION_FORWARD)}

    for team, opponent, opponent_lines, is_home in (
        (match["home"], match["away"], away_lines, True),
        (match["away"], match["home"], home_lines, False),
    ):
        buts_mpg = 0
        goal_events: list[dict] = []
        for p in team["players"].values():
            if p["position"] == POSITION_GOALKEEPER or p["effective_note"] is None:
                p["lines_passed"] = []
                p["but_mpg_bonus"] = False
                p["note_apres_duel"] = p["effective_note"]
            else:
                p.update(_resolve_line_battle(p["effective_note"], p["position"], opponent_lines, is_home, p.get("real_goals", 0)))
            if p["but_mpg_bonus"]:
                buts_mpg += 1
                goal_events.append({"name": p["name"], "type": "mpg", "time": None})
            for t in p.get("real_goal_times", []):
                goal_events.append({"name": p["name"], "type": "real", "time": t, "date": p.get("real_match_date")})
        # CSC reels marques PAR un joueur de l'equipe ADVERSE dans CE match
        # fantasy (retour utilisateur 2026-08-23, correctif majeur -- cf.
        # _own_goals_scored_by_player) : beneficient a CETTE equipe. Scanne
        # opponent["players"] (le XI FINAL resolu de l'adversaire), pas le
        # sien -- un CSC penalise l'equipe fantasy DU BUTEUR, jamais celle qui
        # se contente de partager le meme club reel beneficiaire. Deduplique
        # par eventId (robustesse, meme si un seul joueur peut normalement en
        # marquer un). LIMITE CONNUE (assumee) : si le buteur etait un
        # titulaire D'ORIGINE deja sorti (remplacement tactique/obligatoire,
        # cf. team["out"]) avant l'evenement, il n'est plus une cle de
        # opponent["players"] -- son CSC n'est alors PAS credite/decompte ici
        # (out[] ne porte pas real_own_goal_scored_events). Cas non rencontre
        # dans le signalement d'origine (le buteur n'etait dans AUCUNE des
        # deux equipes) -- a traiter si un cas reel l'exige.
        own_goal_events_seen: dict[str, dict] = {}
        for p in opponent["players"].values():
            for ev in p.get("real_own_goal_scored_events", []):
                eid = ev.get("eventId")
                if eid and eid not in own_goal_events_seen:
                    own_goal_events_seen[eid] = ev
        for ev in own_goal_events_seen.values():
            goal_events.append({"name": "CSC", "type": "real_og", "time": ev.get("time"), "date": ev.get("date")})
        team["buts_mpg"] = buts_mpg
        team["real_goals"] = sum(p.get("real_goals", 0) for p in team["players"].values()) + len(own_goal_events_seen)
        team["score"] = team["real_goals"] + buts_mpg
        team["goal_events"] = goal_events

    # Regle officielle MPG R2 "Arret MPG" (retour utilisateur 2026-08-18,
    # audit collaborateur, confirme avec Ilan) : un gardien a 8/10 ou plus
    # (note EFFECTIVE, bonus compris -- meme lecture que le seuil de 5/10
    # pour marquer, cf. _resolve_line_battle, par symetrie) annule
    # automatiquement UN but adverse (reel ou MPG), JAMAIS un CSC -- deja
    # garanti par apply_removed_goal (n'agit que sur real_goal_times/
    # but_mpg_bonus, jamais sur les entrees "csc"/"real_og" de goal_events).
    # Regle STRUCTURELLE (jamais un "coup" declare par un manager) --
    # appliquee ici dans le calcul OFFICIEL, contrairement aux 7 "coups" de
    # apply_own_bonus_effect/apply_incoming_bonus_effect (cf. docstring
    # module "Coups"), jamais conditionnee a bonus_choices. Doit tourner
    # APRES la boucle ci-dessus (les buts doivent deja etre determines) et
    # AVANT le CSC Rotaldo (qui, de toute facon, ne peut pas etre annule par
    # construction -- ordre choisi pour la lisibilite, pas par necessite).
    for team, opponent in ((match["home"], match["away"]), (match["away"], match["home"])):
        goalkeeper = next((p for p in team["players"].values() if p["position"] == POSITION_GOALKEEPER), None)
        if goalkeeper and goalkeeper.get("effective_note") is not None and goalkeeper["effective_note"] >= 8:
            apply_removed_goal(opponent)

    # CSC Rotaldo : chaque equipe encaisse un but pour l'adverse tous les 3
    # Rotaldo dans son XI final (cf. docstring module) -- ajoute APRES coup,
    # une fois les deux team["score"] etablis, pour ne pas s'auto-influencer.
    #
    # Retour utilisateur 2026-08-17 (a la suite d'un CSC Grotaldo "provisoire"
    # incoherent avec seulement 2 Rotaldo visibles a l'ecran) : "Seuls les
    # joueurs sur le terrain comptent comme un grotaldo... elle imagine un
    # scenario final a un instant T. Si dans le scenario actuel il n'y a que
    # 2 Rotaldo sur le terrain alors pas de CSC Grotaldo" -- rotaldo_count
    # EST deja ce scenario a l'instant T (resolution passe 1 tactique + passe
    # 2 obligatoire sur les donnees actuelles), donc deja "provisoire" par
    # nature (augmente au fil des vrais matchs qui se terminent). Un ANCIEN
    # mecanisme separe (grotaldo_absent_count) anticipait un +1 supplementaire
    # des que 3 titulaires D'ORIGINE etaient a 0 minute, MEME quand un
    # remplacant tactique/obligatoire valide avait deja couvert l'un d'eux
    # (donc bel et bien un joueur SUR LE TERRAIN a ce poste) -- supprime :
    # rotaldo_count est la seule source de verite desormais.
    for team, opponent in ((match["home"], match["away"]), (match["away"], match["home"])):
        confirmed_csc = opponent.get("csc_conceded", 0)
        if confirmed_csc:
            team["score"] += confirmed_csc
            team["goal_events"].append({"name": "CSC (Rotaldo)", "type": "csc", "time": None, "count": confirmed_csc})

    return match


def apply_removed_goal(team: dict) -> None:
    """Valise a Nanard (cf. docstring module) : annule le PREMIER but adverse
    chronologiquement (reel ou MPG), priorite de ligne DF > MF > FW, jamais un
    CSC. Doit etre appele APRES apply_line_battles (les buts doivent deja
    etre determines). Mute `team` en place : score/buts_mpg/real_goals et le
    joueur concerne, plus l'entree correspondante dans goal_events. Un but MPG
    (sans minute) est traite comme survenant apres tout but reel de la meme
    ligne (999) -- pas de vraie minute a comparer, mais "premier" reste
    determine par la priorite de ligne d'abord."""
    for position in (POSITION_DEFENDER, POSITION_MIDFIELDER, POSITION_FORWARD):
        candidates = []
        for p in team["players"].values():
            if p["position"] != position:
                continue
            for t in p.get("real_goal_times", []):
                minute = int(t.rstrip("'")) if t and t.rstrip("'").isdigit() else 999
                candidates.append((minute, "real", p, t))
            if p.get("but_mpg_bonus"):
                candidates.append((999, "mpg", p, None))
        if not candidates:
            continue

        candidates.sort(key=lambda c: c[0])
        _minute, kind, player, time_str = candidates[0]

        if kind == "real":
            player["real_goals"] -= 1
            player["real_goal_times"].remove(time_str)
            team["real_goals"] -= 1
        else:
            player["but_mpg_bonus"] = False
            team["buts_mpg"] -= 1

        team["score"] -= 1
        for i, ev in enumerate(team["goal_events"]):
            if ev["type"] == kind and ev["name"] == player["name"] and (kind != "real" or ev["time"] == time_str):
                del team["goal_events"][i]
                break
        return


def _slot_of_player(team: dict, player_id: str | None) -> str | None:
    if not player_id:
        return None
    for slot, info in team["playersOnPitch"].items():
        if info["playerId"] == player_id:
            return slot
    return None


def _player_in_slot(team: dict, slot: str | None) -> str | None:
    if not slot:
        return None
    info = team["playersOnPitch"].get(slot)
    return info["playerId"] if info else None


def resolve_match_bonus_effects(home_choice: dict | None, away_choice: dict | None, home_team: dict, away_team: dict) -> dict:
    """Prend les coups DECLARES {bonus, targetPlayerId} de chaque cote (ou
    None) et renvoie les effets EFFECTIFS a appliquer, apres redirection
    eventuelle par un Miroir (cf. docstring module). `home_team`/`away_team` :
    team["home"]/team["away"] de get_division_matches() (playersOnPitch), pour
    determiner la cible d'un McDo+ vole par Miroir -- ATTRIBUE AU JOUEUR DE
    MEME NUMERO DE SLOT dans la compo du lanceur du Miroir que le titulaire
    vise chez l'adversaire (retour utilisateur 2026-08-10), pas une cible
    choisie a la main. Renvoie {home: {"self", "opponent", "block_tactical",
    "remove_goal"}, away: {...}} -- "self"/"opponent" sont soit None soit
    {"bonus", "targetPlayerId"}, a passer tels quels a compute_team_live_score
    (own_bonus / incoming_bonus.get("bonus"))."""
    def empty_effect() -> dict:
        return {"self": None, "opponent": None, "block_tactical": False, "remove_goal": False}

    def apply_normal(choice: dict | None, caster_eff: dict, victim_eff: dict) -> None:
        bonus = (choice or {}).get("bonus")
        if not bonus or bonus == "mirror":
            return
        category = _bonus_category(bonus)
        if category == "self":
            caster_eff["self"] = choice
        elif category == "opponent":
            victim_eff["opponent"] = choice
            victim_eff["block_tactical"] = bonus == "blockTacticalSubs"
            victim_eff["remove_goal"] = bonus == "removeGoal"

    def apply_mirrored(original_choice: dict | None, mirror_team: dict, original_team: dict, mirror_eff: dict, original_caster_eff: dict) -> None:
        bonus = (original_choice or {}).get("bonus")
        if not bonus or bonus == "mirror":
            return  # rien a refleter (adversaire sans coup, ou double Miroir) -- no-op.
        category = _bonus_category(bonus)
        if category == "self":
            target_player_id = None
            if bonus == "boostOnePlayer":
                slot = _slot_of_player(original_team, original_choice.get("targetPlayerId"))
                target_player_id = _player_in_slot(mirror_team, slot)
            mirror_eff["self"] = {"bonus": bonus, "targetPlayerId": target_player_id}  # le lanceur du Miroir VOLE l'effet.
        elif category == "opponent":
            original_caster_eff["opponent"] = {"bonus": bonus, "targetPlayerId": None}  # RENVOYE contre son lanceur original.
            original_caster_eff["block_tactical"] = bonus == "blockTacticalSubs"
            original_caster_eff["remove_goal"] = bonus == "removeGoal"

    home_eff, away_eff = empty_effect(), empty_effect()
    home_is_mirror = (home_choice or {}).get("bonus") == "mirror"
    away_is_mirror = (away_choice or {}).get("bonus") == "mirror"

    # Le coup de chaque cote s'applique normalement SAUF si l'AUTRE cote le
    # reflete (auquel cas apply_mirrored s'en charge ci-dessous, a la place --
    # jamais les deux, sinon le coup adverse serait applique en double).
    if not away_is_mirror:
        apply_normal(home_choice, home_eff, away_eff)
    if not home_is_mirror:
        apply_normal(away_choice, away_eff, home_eff)

    if home_is_mirror:
        apply_mirrored(away_choice, home_team, away_team, home_eff, away_eff)
    if away_is_mirror:
        apply_mirrored(home_choice, away_team, home_team, away_eff, home_eff)

    return {"home": home_eff, "away": away_eff}


def compute_division_live_scores(
    division_matches: list[dict], real_matches_by_id: dict[str, dict],
    bonus_choices: dict[str, dict] | None = None,
) -> list[dict]:
    """Applique compute_team_live_score() aux deux equipes de chaque match d'une
    division, puis le duel de lignes (apply_line_battles). Renvoie une liste de
    {matchId, home: {...}, away: {...}} -- "total" de chaque equipe est APRES
    duel de lignes (cf. apply_line_battles). `bonus_choices` : {userId: {"bonus",
    "targetPlayerId"}} des coups declares par les managers (cf. docstring
    module, "Coups") -- optionnel, None/vide = aucun coup applique (comportement
    inchange). Les effets EFFECTIFS de chaque cote (apres redirection Miroir
    eventuelle) sont resolus par resolve_match_bonus_effects ; removeGoal
    (Valise) est applique en dernier, une fois les buts determines par
    apply_line_battles."""
    bonus_choices = bonus_choices or {}
    results = []
    for div_match in division_matches:
        home_user_id = div_match["home"].get("userId")
        away_user_id = div_match["away"].get("userId")
        home_choice = bonus_choices.get(home_user_id)
        away_choice = bonus_choices.get(away_user_id)
        effects = resolve_match_bonus_effects(home_choice, away_choice, div_match["home"], div_match["away"])

        match = {
            "matchId": div_match["id"],
            "home": compute_team_live_score(
                div_match["home"], real_matches_by_id,
                own_bonus=effects["home"]["self"],
                incoming_bonus=(effects["home"]["opponent"] or {}).get("bonus"),
                block_tactical=effects["home"]["block_tactical"],
            ),
            "away": compute_team_live_score(
                div_match["away"], real_matches_by_id,
                own_bonus=effects["away"]["self"],
                incoming_bonus=(effects["away"]["opponent"] or {}).get("bonus"),
                block_tactical=effects["away"]["block_tactical"],
            ),
        }
        results.append(apply_line_battles(match))

        if effects["home"]["remove_goal"]:
            apply_removed_goal(match["home"])
        if effects["away"]["remove_goal"]:
            apply_removed_goal(match["away"])
    return results


def all_real_matches_finished(real_matches_by_id: dict[str, dict]) -> bool:
    """True si TOUS les vrais matchs fournis (typiquement toute une journee
    de division/ligue, cf. collect_real_match_ids) sont au statut TERMINE
    (FINISHED_MATCH_PERIODS) -- False sur dict vide (rien a affirmer). Signal
    utilise par live_watch.py pour figer l'instantane "avant" du plan
    avant/apres (retour utilisateur 2026-08-17, comparaison prediction live vs
    notes MPG definitives -- celles-ci continuent de bouger jusqu'a ~7h apres
    la rencontre, cf. live_scheduler.py APRES_DELAY_HOURS)."""
    return bool(real_matches_by_id) and all(
        m.get("period") in FINISHED_MATCH_PERIODS for m in real_matches_by_id.values()
    )


def is_division_match_final(match: dict) -> bool:
    """True si CE match de DIVISION (get_division_matches, pas un vrai match
    reel) est completement stabilise cote MPG -- rating/bonusesDetails/badges
    surs a lire (cf. docstring get_division_matches, core/api.py). status==2
    ET finalResult=True observes ensemble sur tous les matchs verifies a ce
    jour (2026-08-18, saisons passees) -- les deux verifies par securite."""
    return match.get("status") == 2 and bool(match.get("finalResult"))


def all_division_matches_final(division_matches: list[dict]) -> bool:
    """True si TOUS les matchs d'une division/journee sont finalises (cf.
    is_division_match_final) -- signal a utiliser pour declencher la capture
    "en dur" (archive permanente, retour utilisateur 2026-08-18 : "un appel
    unique qui nous donnera tout ce dont on a besoin pour le live comme pour
    le classement en dur") -- plus fiable qu'un minuteur, contrairement a
    all_real_matches_finished (base sur le VRAI match, utilise pour
    l'instantane "avant" provisoire du plan avant/apres -- celui-ci reste
    utile pour savoir QUAND repoller en attendant que MPG finalise, cf.
    live_scheduler.py)."""
    return bool(division_matches) and all(is_division_match_final(m) for m in division_matches)


def collect_real_match_ids(division_matches: list[dict]) -> set[str]:
    """Union des matchId reels utilises par TOUS les joueurs d'une equipe (XI
    ET banc -- les remplacants ont besoin de leur propre match reel pour
    resoudre les remplacements, cf. resolve_starting_lineup) de tous les
    matchs d'une division -- pour ne fetch chaque match reel qu'une seule fois
    cote appelant, meme s'il est partage par plusieurs joueurs/equipes."""
    match_ids: set[str] = set()
    for div_match in division_matches:
        for side in ("home", "away"):
            team = div_match[side]
            players = team["players"]
            for slot in team["playersOnPitch"].values():
                match_id = players.get(slot["playerId"], {}).get("matchId")
                if match_id:
                    match_ids.add(match_id)
    return match_ids


