"""
Alimente la table Supabase `managers` (userId -> RealName) depuis le
fichier local MPG_Users.json (mpg_app -- aucun endpoint MPG n'expose le
vrai nom d'un manager directement, seulement son nom d'equipe par ligue,
cf. core/api.py::get_division_team_names). Retour utilisateur 2026-08-24,
"on voit toujours un nom d'equipe au lieu du realname" -- meme registre
que celui deja utilise pour verifier le Super Classement contre
Classement_General/Super_Classement_General_saison_*.json cette session.

Filtre sur les managers REELLEMENT actifs cette saison (retour utilisateur
2026-08-24, "je vois que tu utilises parfois TOUS les managers de
MPG_Users.json alors que seuls 72 jouent") -- MPG_Users.json est une liste
d'amis MPG plus large (95 entrees observees, dont des comptes inactifs/
"Actif": false/absent, ou juste des contacts jamais inscrits a une de nos
6 ligues), pas un roster de saison. division_classement_live (deja peuplee
pour les 6 ligues par scripts/live_job.py) est la source de verite : tout
userId qui y apparait a une equipe reelle cette saison -- exactement 72
constate au 2026-08-24, cf. leagues.players_number.

A relancer a la main quand MPG_Users.json change (nouveau manager ajoute
a une ligue) -- pas d'automatisation cron, ce fichier ne bouge que
rarement (retour utilisateur -- source geree manuellement cote mpg_app).

Usage :
    python scripts/seed_managers.py
    python scripts/seed_managers.py C:\chemin\vers\MPG_Users.json
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.live_job import supabase_client

DEFAULT_MPG_USERS_PATH = r"C:\Users\sebas\Desktop\Python\MPG_Users.json"


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MPG_USERS_PATH

    with open(path, encoding="utf-8") as f:
        friends = json.load(f)["friends"]

    sb = supabase_client()

    dcl_rows = sb.table("division_classement_live").select("data").execute().data or []
    active_user_ids = {
        entry["userId"]
        for row in dcl_rows
        for entry in (row.get("data") or [])
        if entry.get("userId")
    }
    print(f"{len(active_user_ids)} userId(s) actif(s) trouve(s) dans division_classement_live")
    if not active_user_ids:
        raise SystemExit("division_classement_live est vide -- rien a filtrer, seed annule (evite de tout effacer par erreur).")

    rows = [
        {"user_id": u["userId"], "real_name": u["RealName"]}
        for u in friends
        if u.get("userId") in active_user_ids and u.get("RealName")
    ]
    print(f"{len(rows)} manager(s) actif(s) trouve(s) dans {path} (sur {len(friends)} au total)")

    missing_real_name = active_user_ids - {r["user_id"] for r in rows}
    if missing_real_name:
        print(f"ATTENTION : {len(missing_real_name)} userId(s) actif(s) sans RealName dans MPG_Users.json : {sorted(missing_real_name)}")

    # Vide la table avant de reinserer -- garantit qu'elle ne contient QUE
    # les managers actifs, jamais un residu d'un seed precedent (retour
    # utilisateur : c'est exactement ce residu qui posait probleme).
    sb.table("managers").delete().neq("user_id", "__never_matches__").execute()

    batch_size = 500
    for i in range(0, len(rows), batch_size):
        sb.table("managers").upsert(rows[i:i + batch_size]).execute()
    print(f"{len(rows)} manager(s) ecrits dans Supabase (table managers).")


if __name__ == "__main__":
    main()
