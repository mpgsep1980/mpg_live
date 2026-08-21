# mpg_live -- scaffold (rien n'est deploye)

Brouillon local pour la version "en ligne" de mpg_app : GitHub Actions comme
moteur de calcul (reutilise `core/live_scoring.py` sans le reecrire),
Supabase comme stockage + source de donnees pour un site statique (GitHub
Pages). Aucun compte/service externe n'a ete cree ou touche pour produire ce
scaffold -- tout est local, a relire avant de brancher quoi que ce soit de
reel.

## Ce qui existe ici

- `db/schema.sql` -- tables Postgres proposees (leagues, gameweek_state,
  live_snapshots, league_classement_archive, super_classement,
  general_bonus_config, accounts) + fonctions RPC pour l'auth manager
  (verify_manager_password / set_manager_password, via pgcrypto).
- `core/live_scoring.py` -- copie strictement identique de mpg_app (module
  autonome, aucune dependance a un fichier local).
- `core/api.py` -- copie identique de mpg_app (appels a l'API MPG).
- `core/token.py` -- adapte : lit `MPG_TOKEN` depuis l'environnement au lieu
  d'un fichier local (n'a de sens que dans mpg_app, ou tourne sur ta
  machine).
- `scripts/live_job.py` -- fusion de `live_scheduler.py` + `live_watch.py`
  de mpg_app en UN SEUL tick idempotent : consulte le calendrier, poll les
  ligues dont la fenetre de journee est ouverte, ecrit dans Supabase. Pas de
  boucle infinie -- la repetition vient du cron GitHub Actions.
- `.github/workflows/live_job.yml` -- cron toutes les 10 min, lance
  `scripts/live_job.py` avec 3 secrets (`MPG_TOKEN`, `SUPABASE_URL`,
  `SUPABASE_KEY`).
- `site/manager.html` -- exemple de lecture cote navigateur (supabase-js +
  cle anonyme), affiche les instantanes bruts de `live_snapshots`. Ne
  represente PAS encore le classement complet avec bonus (Pichichi, Le Mur,
  bonus generaux...) -- cf. "Ce qui manque" ci-dessous.

## Ce qui manque avant de pouvoir deployer

1. **`core/league.py`, `core/general_bonus.py`, `core/live_projection.py`**
   pas encore portes -- ils lisent des fichiers JSON locaux cote mpg_app.
   `scripts/live_job.py` les contourne pour l'instant (fonction
   `get_all_leagues()` locale qui lit la table `leagues`), mais la fusion
   classement + bonus generaux (`live_projection.py`, `general_bonus.py`,
   ~670 lignes a deux) n'a pas d'equivalent Supabase pour l'instant --
   `site/manager.html` n'affiche donc que des scores de matchs bruts, pas un
   classement.
2. **Un vrai projet Supabase** -- executer `db/schema.sql`, configurer les
   policies RLS (lecture publique sur les tables de classement, ecriture
   reservee au role `service_role`), `grant execute` sur les 2 fonctions RPC.
3. **Un repo GitHub public** -- pousser ce dossier, ajouter les 3 secrets
   Actions (`MPG_TOKEN`, `SUPABASE_URL`, `SUPABASE_KEY` -- cette derniere
   doit etre la cle `service_role`, jamais l'anonyme).
4. **Peupler la table `leagues`** -- une fois le schema applique, il faut
   inserer les 6 lignes (equivalent de `League_Codes.json`) a la main ou via
   un script d'import ponctuel.
5. **Porter les 7 autres pages HTML** (`super_classement.html`,
   `poules.html`, `comptes.html`...) sur le meme pattern que
   `site/manager.html`.
6. **GitHub Pages** -- activer sur le repo une fois `site/` pret.

Rien de tout ca n'est fait -- ce scaffold sert a valider l'approche (schema +
script + workflow) avant de commencer.
