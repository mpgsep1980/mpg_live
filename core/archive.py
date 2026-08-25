"""
Archivage de fin de journee -- cumule le resultat FINAL d'une journee dans
league_classement_archive. Appele depuis scripts/live_job.py (cron, une fois
window_end depasse) ET scripts/backfill_gameweek.py (rattrapage manuel d'une
journee jamais pollee en direct) -- MEME fonction pour les deux cas depuis le
retour utilisateur 2026-08-24 ("les resultats peuvent changer 1-2 jours apres
... avoir une possibilite d'aller rechercher ces journees modifiees") : la
source n'est plus le dernier live_snapshots pollee EN DIRECT (pouvait
capturer un etat MPG pas encore stabilise -- notes/buts encore susceptibles
de changer), mais TOUJOURS core.archive_capture.capture_division_journee
(attend all_division_matches_final avant d'ecrire quoi que ce soit) --
retente automatiquement au prochain appel si MPG n'a pas fini de stabiliser,
jamais d'ecriture prematuree. Le JSON capture est aussi conserve dans
live_snapshots (memes PK que le live) : c'est CE stockage permanent par
journee qui rend rebuild_division_archive possible ci-dessous.

Idempotent par construction (is_gameweek_archived) pour le cas "premiere
archive" : peut etre appele a chaque tick sans jamais double-compter.

Pour CORRIGER une journee DEJA archivee (resultat MPG modifie apres coup --
changement de buteur, but reclasse CSC, cf. retour utilisateur 2026-08-24),
voir plutot rebuild_division_archive : la fusion incrementale base+delta de
combined_division_standings est irreversible en soi (l'archive ne retient
qu'un cumul, pas le detail par journee) -- rejouer juste la journee corrigee
par-dessus le cumul existant la compterait deux fois si d'autres journees
ont deja ete archivees par-dessus depuis. rebuild_division_archive ne fusionne
jamais : il REMPLACE le cumul en rejouant TOUT l'historique connu depuis
zero, donc corrige proprement quel que soit le nombre de journees deja
archivees par-dessus.
"""
from datetime import datetime, timezone

from core.archive_capture import capture_division_journee, dtc_counts_from_matches, RAW_BONUS_TO_DTC_KEY
from core.live_scoring import all_division_matches_final
from core.internal_bonus import compute_internal_bonuses
from core.live_projection import (
    is_gameweek_archived, division_delta, combined_division_standings, league_setup,
)

DTC_KEYS = tuple(RAW_BONUS_TO_DTC_KEY.values())


def _stats_from_row(
    row: dict, precious_holder_user_id: str | None = None, previous_precious_count: int = 0,
    previous_dtc: dict | None = None, dtc_this_gameweek: dict | None = None,
) -> dict:
    """Convertit une ligne de sortie de combined_division_standings (cles
    buts_pour/buts_contre/victory/...) vers la forme stockee en base
    (score+/score-/victory/..., cf. db/schema.sql) -- la meme forme que
    load_base_division_classement lit en entree, necessaire pour pouvoir
    enchainer plusieurs journees d'affilee dans rebuild_division_archive.
    pichichi/mur/bonus_champion/bonus_podium/precious AJOUTES (retour
    utilisateur 2026-08-24, "recupere la logique de points qu'on a definie
    dans le document partage avec Ilan, on n'a pas besoin de reinventer la
    roue") -- formule officielle retrouvee dans le notebook de production
    (calculate_total_points, MPG_Ligue_2_EKT_Test_2025.ipynb) : total =
    points_pond + Pichichi + Le_Mur + cleanSheet + manita + Bonus_Podium +
    Bonus_Champion + on_fire + Precious - grotaldo. core.internal_bonus.
    compute_internal_bonuses (deja audite/corrige avec Ilan, deja utilise
    tel quel par le chemin LIVE de resolve_division_rows) est maintenant
    AUSSI appele a l'archivage, au lieu de laisser ces bonus retomber a 0
    des qu'une journee est archivee -- meme solution que celle documentee
    dans Reponse_audit_pour_Ilan.md section "Regeneration de l'archive en
    cours de saison" (recalculer depuis les matchs bruts, jamais improviser
    une nouvelle formule)."""
    bonus = row.get("bonus_details") or {}
    return {
        "teamName": row.get("teamName", ""),
        "victory": row["victory"], "draw": row["draw"], "defeat": row["defeat"],
        "matches_joues": row["matches_joues"],
        "score+": row["buts_pour"], "score-": row["buts_contre"],
        "points_pond": row["points_pond"],
        "cleanSheet": row["cleanSheet"], "manita": row["manita"], "on_fire": row["on_fire"],
        "grotaldo": row["grotaldo"], "owngoals": row["owngoals"],
        "Ycards": row.get("Ycards", 0), "Rcards": row.get("Rcards", 0),
        "pichichi": bonus.get("Pichichi", 0), "mur": bonus.get("Le_Mur", 0),
        "bonus_champion": bonus.get("Bonus_Champion", 0), "bonus_podium": bonus.get("Bonus_Podium", 0),
        # Rang COMPLET (departage a 6 criteres, cf. core/internal_bonus.py::
        # compute_internal_bonuses -- "_full_rank") persiste ici pour que
        # refresh_division_classement_from_archive/_rows_from_archive (qui
        # n'ont plus acces a division_matches une fois l'archive relue)
        # puissent quand meme afficher le bon rang, au lieu de retomber sur
        # un tri partiel a 3 criteres (retour utilisateur 2026-08-25, "il
        # semble qu'il y ait des soucis de priorisation").
        "rang": row.get("_full_rank", 0),
        # Bonus_Second/Bonus_Dernier -- meme cas que Boss_Saison ci-dessous :
        # deja calcules par compute_internal_bonuses, jamais surfaces avant
        # ce correctif (retour utilisateur 2026-08-24) -- necessaires pour
        # activer Poulidor/La Chèvre dans les bonus generaux (cf.
        # core/general_bonus.py, compute_super_classement).
        "bonus_second": bonus.get("Bonus_Second", 0), "bonus_dernier": bonus.get("Bonus_Dernier", 0),
        "precious": 1 if precious_holder_user_id and row["userId"] == precious_holder_user_id else 0,
        # Precious_Count -- cumul saison (pas juste "detenteur actuel comme
        # precious ci-dessus) : Gollum recompense qui a detenu le Precieux le
        # PLUS SOUVENT, pas qui l'a en ce moment (retour utilisateur
        # 2026-08-24, "documente dans le Git avec Ilan" -- Corrections_pour_
        # Sep.md section B3, "en incrementant la valeur archivee"). Repose
        # sur `previous_precious_count` (l'archive PRECEDENTE de ce meme
        # manager) plutot qu'un recalcul depuis l'historique complet --
        # meme logique cumulative que victory/draw/defeat dans
        # combined_division_standings, juste tenue ici plutot que la-bas
        # (precious_holder_user_id n'est connu qu'a l'archivage, jamais en
        # cours de journee).
        "precious_count": previous_precious_count + (
            1 if precious_holder_user_id and row["userId"] == precious_holder_user_id else 0
        ),
        # Boss_Saison -- deja calcule par compute_internal_bonuses (cle
        # "{league_name}_Boss_Saison_{season_number}", jamais surfacee
        # jusqu'ici) : necessaire pour Multi Boss/La Triplette au niveau du
        # Super Classement (retour utilisateur 2026-08-24, meme retrouve
        # dans Super_Classement_General_V2.ipynb cellule 6). Recherche par
        # sous-chaine plutot que cle exacte : _stats_from_row ne connait ni
        # league_name ni season_number, et un seul "_Boss_Saison_" peut
        # exister par appel (une ligue/saison a la fois).
        "boss_saison": next((v for k, v in bonus.items() if "_Boss_Saison_" in k), 0),
        # *_DTC (8 compteurs, La Piñata) -- meme principe cumulatif que
        # precious_count : base (previous_dtc) + delta de cette seule
        # journee (dtc_this_gameweek, cf. core/archive_capture.py::
        # dtc_counts_from_matches, retour utilisateur 2026-08-24, "cumul du
        # nombre de coups/bonus subis... en theorie c'est documente").
        **{
            key: (previous_dtc or {}).get(key, 0) + (dtc_this_gameweek or {}).get(key, 0)
            for key in DTC_KEYS
        },
    }


def archive_closed_gameweek_if_needed(sb, league: dict, closed_game_week: int, total_divisions: int) -> int:
    """Archive la journee `closed_game_week` de `league`, division par
    division, si ce n'est pas deja fait. Capture elle-meme les donnees
    MPG-finalisees (plus besoin d'un live_snapshots deja pollee en amont) --
    n'ecrit rien pour une division dont MPG n'a pas encore tout stabilise
    (retente au prochain appel). Renvoie le nombre de divisions dans un etat
    "fini" CETTE fois -- fraichement archivees OU deja archivees lors d'un
    appel precedent (idempotent, cf. is_gameweek_archived) -- sur
    total_divisions ; seule une division dont MPG n'a toujours pas fini de
    stabiliser N'est PAS comptee, pour que l'appelant sache s'il faudra
    reessayer plus tard sans jamais confondre "deja fait" et "pas encore
    pret"."""
    short_id = league["code"]
    season = league["seasonSearch"]
    done = 0

    # Une seule fois pour toute la ligue (internal_cfg/last_gameweek/
    # league_size ne dependent pas de la division) -- meme fonction que le
    # chemin live (scripts/live_job.py::poll_league), reutilisee ici pour ne
    # pas dupliquer le calcul de last_gameweek (cf. league_setup, cache 1h).
    setup = league_setup(sb, league, list(range(1, total_divisions + 1)), closed_game_week)

    for division in range(1, total_divisions + 1):
        base_by_user = setup["base_by_division"].get(division, {})

        capture = capture_division_journee(short_id, season, division, closed_game_week)
        if capture is None:
            continue  # MPG pas encore stabilise -- reessai au prochain appel

        division_matches = capture["divisionMatches"]
        user_ids = [m[side].get("userId") for m in division_matches for side in ("home", "away")]

        if is_gameweek_archived(base_by_user, user_ids, closed_game_week):
            done += 1
            continue

        precious_holder_user_id = capture.get("preciousHolderUserId")
        now = datetime.now(timezone.utc).isoformat()
        sb.table("live_snapshots").upsert({
            "league_code": short_id, "season": season, "game_week": closed_game_week,
            "division": division, "data": division_matches,
            "precious_holder_user_id": precious_holder_user_id, "updated_at": now,
        }).execute()

        delta_rows = division_delta(division_matches, division, closed_game_week, league, total_divisions)
        combined = combined_division_standings(base_by_user, delta_rows)
        compute_internal_bonuses(
            combined, division, closed_game_week, setup["last_gameweek"],
            setup["league_size"], setup["season_number"], setup["season_start"], league["nom"],
            setup["internal_cfg"], division_matches=division_matches,
        )
        dtc_by_user = dtc_counts_from_matches(division_matches)

        for row in combined:
            previous = base_by_user.get(row["userId"], {})
            sb.table("league_classement_archive").upsert({
                "league_code": short_id, "season": season, "division": division,
                "user_id": row["userId"],
                "stats": _stats_from_row(
                    row, precious_holder_user_id, previous.get("precious_count", 0),
                    previous, dtc_by_user.get(row["userId"]),
                ),
                "updated_at": now,
            }).execute()
        done += 1

    return done


def rebuild_division_archive(sb, league: dict, division: int, total_divisions: int) -> int:
    """Reconstruit ENTIEREMENT league_classement_archive pour une division a
    partir de TOUT l'historique connu dans live_snapshots (pas un merge
    incremental) -- seul moyen sur d'incorporer une correction MPG survenue
    sur une journee deja archivee (retour utilisateur 2026-08-24). Ne rejoue
    QUE les journees dont data est bien "finalise" (all_division_matches_final)
    -- ecarte silencieusement toute ligne live/en cours qui trainerait encore
    sous le meme PK (ne peut pas arriver une fois qu'une journee est
    finalisee : son upsert ecrase definitivement la forme live par la forme
    MPG-finalisee, cf. archive_closed_gameweek_if_needed, mais reste une
    securite peu couteuse si applique a une division jamais archivee).
    Remplace (jamais ne fusionne) les lignes existantes de
    league_classement_archive pour cette division. Renvoie le nombre de
    journees effectivement rejouees (0 si aucune journee finalisee connue)."""
    short_id = league["code"]
    season = league["seasonSearch"]

    snap_res = (
        sb.table("live_snapshots").select("game_week, data, precious_holder_user_id")
        .eq("league_code", short_id).eq("season", season).eq("division", division)
        .order("game_week")
        .execute()
    )
    final_snapshots = [
        (row["game_week"], row["data"], row.get("precious_holder_user_id")) for row in snap_res.data
        if all_division_matches_final(row["data"])
    ]
    if not final_snapshots:
        return 0

    # last_gameweek/internal_cfg calcules une seule fois pour toute la
    # reconstruction (ne dependent pas de la journee rejouee) -- game_week
    # de reference = la plus recente connue, meme convention que le tick
    # live (league_setup lit toujours "aujourd'hui", pas une journee passee).
    setup = league_setup(sb, league, [division], final_snapshots[-1][0])

    base_by_user: dict[str, dict] = {}
    for game_week, division_matches, precious_holder_user_id in final_snapshots:
        delta_rows = division_delta(division_matches, division, game_week, league, total_divisions)
        combined_rows = combined_division_standings(base_by_user, delta_rows)
        compute_internal_bonuses(
            combined_rows, division, game_week, setup["last_gameweek"],
            setup["league_size"], setup["season_number"], setup["season_start"], league["nom"],
            setup["internal_cfg"], division_matches=division_matches,
        )
        dtc_by_user = dtc_counts_from_matches(division_matches)
        base_by_user = {
            row["userId"]: _stats_from_row(
                row, precious_holder_user_id, base_by_user.get(row["userId"], {}).get("precious_count", 0),
                base_by_user.get(row["userId"], {}), dtc_by_user.get(row["userId"]),
            )
            for row in combined_rows
        }

    now = datetime.now(timezone.utc).isoformat()
    for user_id, stats in base_by_user.items():
        sb.table("league_classement_archive").upsert({
            "league_code": short_id, "season": season, "division": division,
            "user_id": user_id, "stats": stats, "updated_at": now,
        }).execute()

    return len(final_snapshots)
