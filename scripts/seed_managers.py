"""
Alimente la table Supabase `managers` (userId -> RealName) depuis le
fichier local MPG_Users.json (mpg_app -- aucun endpoint MPG n'expose le
vrai nom d'un manager directement, seulement son nom d'equipe par ligue,
cf. core/api.py::get_division_team_names). Retour utilisateur 2026-08-24,
"on voit toujours un nom d'equipe au lieu du realname" -- meme registre
que celui deja utilise pour verifier le Super Classement contre
Classement_General/Super_Classement_General_saison_*.json cette session.

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

    rows = [
        {"user_id": u["userId"], "real_name": u["RealName"]}
        for u in friends if u.get("userId") and u.get("RealName")
    ]
    print(f"{len(rows)} manager(s) trouve(s) dans {path}")

    sb = supabase_client()
    # Upsert par lots de 500 -- la table peut depasser largement 72 lignes
    # (pool historique multi-ligues, MPG_Users.json en contient bien plus
    # que les seuls managers actuellement suivis).
    batch_size = 500
    for i in range(0, len(rows), batch_size):
        sb.table("managers").upsert(rows[i:i + batch_size]).execute()
    print(f"{len(rows)} manager(s) ecrits dans Supabase (table managers).")


if __name__ == "__main__":
    main()
