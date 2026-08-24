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

from core.archive_capture import capture_division_journee
from core.live_scoring import all_division_matches_final
from core.live_projection import (
    load_base_division_classement, is_gameweek_archived, division_delta, combined_division_standings,
)


def _stats_from_row(row: dict) -> dict:
    """Convertit une ligne de sortie de combined_division_standings (cles
    buts_pour/buts_contre/victory/...) vers la forme stockee en base
    (score+/score-/victory/..., cf. db/schema.sql) -- la meme forme que
    load_base_division_classement lit en entree, necessaire pour pouvoir
    enchainer plusieurs journees d'affilee dans rebuild_division_archive."""
    return {
        "teamName": row.get("teamName", ""),
        "victory": row["victory"], "draw": row["draw"], "defeat": row["defeat"],
        "matches_joues": row["matches_joues"],
        "score+": row["buts_pour"], "score-": row["buts_contre"],
        "points_pond": row["points_pond"],
        "cleanSheet": row["cleanSheet"], "manita": row["manita"], "on_fire": row["on_fire"],
        "grotaldo": row["grotaldo"], "owngoals": row["owngoals"],
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

    for division in range(1, total_divisions + 1):
        base_by_user = load_base_division_classement(sb, short_id, season, division)

        capture = capture_division_journee(short_id, season, division, closed_game_week)
        if capture is None:
            continue  # MPG pas encore stabilise -- reessai au prochain appel

        division_matches = capture["divisionMatches"]
        user_ids = [m[side].get("userId") for m in division_matches for side in ("home", "away")]

        if is_gameweek_archived(base_by_user, user_ids, closed_game_week):
            done += 1
            continue

        now = datetime.now(timezone.utc).isoformat()
        sb.table("live_snapshots").upsert({
            "league_code": short_id, "season": season, "game_week": closed_game_week,
            "division": division, "data": division_matches, "updated_at": now,
        }).execute()

        delta_rows = division_delta(division_matches, division, closed_game_week, league, total_divisions)
        combined = combined_division_standings(base_by_user, delta_rows)

        for row in combined:
            sb.table("league_classement_archive").upsert({
                "league_code": short_id, "season": season, "division": division,
                "user_id": row["userId"], "stats": _stats_from_row(row), "updated_at": now,
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
        sb.table("live_snapshots").select("game_week, data")
        .eq("league_code", short_id).eq("season", season).eq("division", division)
        .order("game_week")
        .execute()
    )
    final_snapshots = [
        (row["game_week"], row["data"]) for row in snap_res.data
        if all_division_matches_final(row["data"])
    ]
    if not final_snapshots:
        return 0

    base_by_user: dict[str, dict] = {}
    for game_week, division_matches in final_snapshots:
        delta_rows = division_delta(division_matches, division, game_week, league, total_divisions)
        combined_rows = combined_division_standings(base_by_user, delta_rows)
        base_by_user = {row["userId"]: _stats_from_row(row) for row in combined_rows}

    now = datetime.now(timezone.utc).isoformat()
    for user_id, stats in base_by_user.items():
        sb.table("league_classement_archive").upsert({
            "league_code": short_id, "season": season, "division": division,
            "user_id": user_id, "stats": stats, "updated_at": now,
        }).execute()

    return len(final_snapshots)
