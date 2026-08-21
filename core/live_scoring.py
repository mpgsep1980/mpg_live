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

Recompense : si un joueur passe TOUTES ses lignes (gardien adverse compris),
il marque un "but MPG" pour son EQUIPE -- pas de bareme de points invente ici
(pas de "but classique" : les seuls buts avec un bareme sont les vrais buts
marques dans les matchs reels, deja captes par mpgRating cf. plus haut). Le but
MPG est donc juste compte (team["buts_mpg"]), pas ajoute a la note du joueur --
seule la penalite de la derniere ligne (le duel contre le gardien) s'applique
a sa note, comme n'importe quelle autre ligne franchie. EXCEPTION : un joueur
ayant deja marque un but REEL dans son match (stats.goals >= 1, cf.
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

Capitaine : le bonus capitaine (+0.5, deja dans note_finale du titulaire
d'origine) n'est PAS transfere si le capitaine est remplace (tactique ou
obligatoire) -- il est juste perdu pour l'equipe ce match-la (regle explicite
: "tu peux faire sortir ce joueur, tant pis pour ses +0.5pt").

Rotaldo / CSC : chaque slot en Rotaldo compte dans team["rotaldo_count"].
Tous les 3 Rotaldo dans le XI final, l'equipe encaisse un CSC (but contre son
camp) -- implemente comme +1 au SCORE DE L'ADVERSAIRE (team["csc_conceded"] =
rotaldo_count // 3), coherent avec le sens reel de "CSC" (but marque pour
l'autre equipe), ajoute au tableau de bord (apply_line_battles).

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
  equipe (gardien compris, aucune exclusion mentionnee dans le reglement).
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

ROTALDO_NOTE = 2.5

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
    """Cherche `player_id` dans /championship-match/{matchId} (home ou away)."""
    for side in ("home", "away"):
        rp = real_match[side]["players"].get(player_id)
        if rp:
            return rp
    return None


def _goal_times_for(real_match: dict, player_id: str) -> list[str]:
    """Minutes des buts REELS de `player_id` dans `real_match` -- evenements
    home["goals"]/away["goals"] (liste {scorerId, time, ...}), plus fiable que
    de juste compter stats.goals (donne aussi la minute pour l'affichage)."""
    return [
        event.get("time", "")
        for side in ("home", "away")
        for event in (real_match[side].get("goals") or [])
        if event.get("scorerId") == player_id
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

    position = player_slot.get("position")
    bonus_formation = formation_defender_bonus(position, composition)
    bonus_cap = captain_bonus if is_captain else 0.0
    note_finale = (note + bonus_formation + bonus_cap) if note is not None else None

    return {
        "name": f"{player_slot.get('firstName', '')} {player_slot.get('lastName', '')}".strip(),
        "position": position,
        "real_goals": real_goals,
        "real_goal_times": real_goal_times,
        "note": note,
        "minutes_played": minutes,
        "bonus_formation_defenseurs": bonus_formation,
        "bonus_capitaine": bonus_cap,
        "note_finale": note_finale,
    }


def _is_played(player_computed: dict) -> bool:
    return (player_computed.get("minutes_played") or 0) > 0


def resolve_starting_lineup(team: dict, real_matches_by_id: dict[str, dict], block_tactical: bool = False) -> dict:
    """Resout les remplacements (tactiques puis obligatoires, cf. docstring
    module) pour les 11 titulaires de `team`. `block_tactical` : Tonton Pat'
    joue par l'ADVERSAIRE (ou renvoye par un Miroir, cf. docstring module) --
    saute la passe 1 (tactique) pour les slots pas deja resolus par MPG, la
    passe 2 (obligatoire) reste active. Renvoie {players, rotaldo_count,
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
            resolution[starter_id] = {"playerId": slot_pid, "source": "tactique", "poste_sautes": 0}

    # Passe 1 : remplacements tactiques (prioritaires), jamais pour un gardien.
    # Si le remplacant designe n'a pas joue, il n'y a personne a faire entrer --
    # le remplacement tactique ne se fait PAS (le titulaire reste, cf. retour
    # utilisateur 2026-08-08 -- le risque du tactique, c'est une note moins
    # bonne que prevu chez le remplacant, PAS son absence de jeu). Ignore les
    # slots deja resolus par MPG ci-dessus (resolution deja renseignee), et la
    # passe entiere si Tonton Pat' bloque (block_tactical, cf. docstring).
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
        if note is None or note < ts["rating"]:
            used_ids.add(ts["subId"])
            resolution[starter_id] = {"playerId": ts["subId"], "source": "tactique", "poste_sautes": 0}

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

        position = starter["position"]
        candidate_positions = [position] if position == POSITION_GOALKEEPER else list(range(position, POSITION_DEFENDER - 1, -1))
        found = None
        for poste_sautes, cand_position in enumerate(candidate_positions):
            for bench_id in bench_ids:
                if bench_id in used_ids:
                    continue
                cand = computed.get(bench_id)
                if cand and cand["position"] == cand_position and _is_played(cand):
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

        final_id = res["playerId"]
        final_player = computed[final_id]
        penalty = float(res["poste_sautes"])
        is_rotaldo = final_player["note_finale"] is None
        effective_note = ROTALDO_NOTE if is_rotaldo else round(final_player["note_finale"] - penalty, 2)
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
            starters_out.append({
                "playerId": starter_id, "name": starter_info["name"], "position": starter_info["position"],
                "note_finale": starter_info["note_finale"], "minutes_played": starter_info["minutes_played"],
                "reason": res["source"],  # "tactique" ou "obligatoire"
                "replaced_by": final_player["name"],
            })

    bench = []
    for pid in bench_ids:
        info = computed.get(pid)
        if not info:
            continue
        bench.append({
            "playerId": pid, "name": info["name"], "position": info["position"],
            "note_finale": info["note_finale"], "minutes_played": info["minutes_played"],
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
    pas vraiment de sens")."""
    if bonus == "boostAllPlayers":
        for p in players.values():
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
    block_tactical si Tonton Pat' cible cette equipe), puis applique les coups
    de note declares (own_bonus = {"bonus", "targetPlayerId"} de CETTE equipe,
    incoming_bonus = cle du coup joue par l'ADVERSAIRE, cf. docstring module)
    -- "total" est la somme des effective_note APRES coups, PRE duel de
    lignes. removeGoal (Valise) n'est PAS applique ici -- il agit sur les buts,
    determines seulement apres apply_line_battles, cf. compute_division_live_scores."""
    lineup = resolve_starting_lineup(team, real_matches_by_id, block_tactical=block_tactical)
    if own_bonus:
        apply_own_bonus_effect(lineup["players"], own_bonus.get("bonus"), own_bonus.get("targetPlayerId"))
    if incoming_bonus:
        apply_incoming_bonus_effect(lineup["players"], incoming_bonus)
    total = sum(p["effective_note"] for p in lineup["players"].values())

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
    tout duel (cf. docstring module)."""
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
    goal_scored = reached_keeper and real_goals == 0

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
    ["real_goals"]/["score"] (real_goals + buts_mpg + CSC Rotaldo concedes par
    l'adverse), team["goal_events"] (liste {name, type: "real"|"mpg"|"csc",
    time} triable pour un affichage type feuille de match) et
    team["line_averages"] (moyennes DF/MF/FW de SA PROPRE equipe -- cle =
    POSITION_DEFENDER/MIDFIELDER/FORWARD, deja calculees pour le duel de
    lignes, exposees ici pour l'affichage cote UI, cf. retour utilisateur
    2026-08-09)."""
    home_lines = {pos: _line_average(match["home"]["players"], pos) for pos in (POSITION_DEFENDER, POSITION_MIDFIELDER, POSITION_FORWARD, POSITION_GOALKEEPER)}
    away_lines = {pos: _line_average(match["away"]["players"], pos) for pos in (POSITION_DEFENDER, POSITION_MIDFIELDER, POSITION_FORWARD, POSITION_GOALKEEPER)}

    match["home"]["line_averages"] = {pos: home_lines[pos] for pos in (POSITION_DEFENDER, POSITION_MIDFIELDER, POSITION_FORWARD)}
    match["away"]["line_averages"] = {pos: away_lines[pos] for pos in (POSITION_DEFENDER, POSITION_MIDFIELDER, POSITION_FORWARD)}

    for team, opponent_lines, is_home in ((match["home"], away_lines, True), (match["away"], home_lines, False)):
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
                goal_events.append({"name": p["name"], "type": "real", "time": t})
        team["buts_mpg"] = buts_mpg
        team["real_goals"] = sum(p.get("real_goals", 0) for p in team["players"].values())
        team["score"] = team["real_goals"] + buts_mpg
        team["goal_events"] = goal_events

    # CSC Rotaldo : chaque equipe encaisse un but pour l'adverse tous les 3
    # Rotaldo dans son XI final (cf. docstring module) -- ajoute APRES coup,
    # une fois les deux team["score"] etablis, pour ne pas s'auto-influencer.
    for team, opponent in ((match["home"], match["away"]), (match["away"], match["home"])):
        csc = opponent.get("csc_conceded", 0)
        if csc:
            team["score"] += csc
            team["goal_events"].append({"name": "CSC (Rotaldo)", "type": "csc", "time": None, "count": csc})

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
