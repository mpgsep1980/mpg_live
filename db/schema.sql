-- Schema Supabase (Postgres) pour mpg_live.
-- A relire et ajuster AVANT toute execution sur un vrai projet Supabase --
-- rien n'a ete applique nulle part, ce fichier est juste la proposition.
--
-- Remplace les fichiers JSON locaux de mpg_app (League_Codes.json,
-- live_scheduler_state.json, live_snapshots/*.json, classements archives,
-- general_bonus_config.json, manager_accounts.json).

-- ============================================================
-- Ligues suivies -- remplace League_Codes.json
-- ============================================================
create table leagues (
    code                  text primary key,       -- shortId MPG (ex. QLRBXDCX2)
    nom                   text not null,           -- nom interne (ex. Ligue_2_EKT)
    championship_id       integer,                 -- id championnat reel (calendrier football)
    season_search         integer,                 -- saison MPG (seasonSearch)
    season_start          integer,                 -- 1ere saison MPG suivie par cette ligue (pour Boss_Saison)
    players_number        integer,                 -- taille totale de la ligue (ex. 72), distinct de players_per_division
    players_per_division  integer default 8,
    pool_gameweeks        integer,                 -- longueur de la phase de poules (repli si le calendrier MPG est indisponible)
    div_a_gameweeks       integer,                 -- longueur de la phase A (meme repli)
    scoring               jsonb not null default '{}'::jsonb,  -- surcharges scoring/bonus (cf. core/league.py::get_scoring_config cote mpg_app)
    updated_at            timestamptz not null default now()
);

-- ============================================================
-- Etat "journee en cours" par ligue -- remplace live_scheduler_state.json
-- Une fenetre (window_start/window_end) peut s'etaler sur 4-5 jours reels ;
-- scripts/live_job.py relit cette table a chaque tick de cron pour savoir
-- s'il doit poller cette ligue ce tour-ci.
-- ============================================================
create table gameweek_state (
    league_code     text primary key references leagues(code),
    game_week       integer not null,
    window_start    timestamptz not null,
    window_end      timestamptz not null,
    last_polled_at  timestamptz,
    updated_at      timestamptz not null default now()
);

-- ============================================================
-- Instantanes live -- remplace Classement_General/live_snapshots/*.json
-- Un upsert par division a chaque tick de cron pendant la fenetre active.
-- `data` = resultat brut de core.live_scoring.compute_division_live_scores
-- (liste de matchs {home, away, ...}), pas encore fusionne en classement.
-- ============================================================
create table live_snapshots (
    league_code  text not null references leagues(code),
    season       integer not null,
    game_week    integer not null,
    division     integer not null,
    data         jsonb not null,
    updated_at   timestamptz not null default now(),
    primary key (league_code, season, game_week, division)
);

-- ============================================================
-- Classements archives (fin de journee/saison) -- remplace
-- <Ligue>/Classement/<saison>/<Ligue>_Classement_General_saison_*.json
--
-- Ecrite par scripts/live_job.py (core/archive.py::archive_closed_gameweek_if_needed)
-- une fois qu'une journee est terminee (window_end depasse), jamais par le
-- site. `stats` est un sous-ensemble volontairement reduit de ce que
-- mpg_app archive (pas de Precieux/Grotaldo -- hors perimetre v1, voir
-- core/live_projection.py cote mpg_app pour le futur port complet) :
--   {
--     "teamName":      text,   -- nom d'equipe MPG (pas de registre managers en v1)
--     "victory":       int,
--     "draw":          int,
--     "defeat":        int,
--     "matches_joues":  int,
--     "score+":        int,    -- buts pour cumules
--     "score-":        int,    -- buts contre cumules
--     "points_pond":   numeric, -- points ponderes cumules, pour rang_ligue
--     "cleanSheet":    int,
--     "manita":        int,
--     "on_fire":       int
--   }
-- ============================================================
create table league_classement_archive (
    league_code  text not null references leagues(code),
    season       integer not null,
    division     integer not null,
    user_id      text not null,
    stats        jsonb not null,
    updated_at   timestamptz not null default now(),
    primary key (league_code, season, division, user_id)
);

-- ============================================================
-- Classement de division pret a afficher (base archivee + delta live
-- combines et deja passes par compute_internal_bonuses) -- alimente
-- site/division.html. Une ligne par division, upsertee a chaque tick de
-- cron ; PAS de colonne game_week dans la cle, le site lit toujours "la"
-- ligne courante sans tri/limit. Ecrite uniquement par scripts/live_job.py
-- (core/live_projection.py::resolve_division_rows +
-- resolve_league_wide_ranks), jamais par le site.
--
-- `data` = tableau classe, un element par manager :
--   {
--     "userId":        text,
--     "teamName":      text,
--     "rang":          int,     -- rang de division (1-indexe)
--     "points":        int,     -- points BRUTS de division (V*3+N*1), pas ponderes
--     "matches_joues": int,
--     "victoires":     int, "nuls": int, "defaites": int,
--     "buts_pour":     int, "buts_contre": int, "diff": int,
--     "cleanSheet":    int, "manita": int, "on_fire": int,
--     "pichichi":      numeric, -- valeur courante du bonus (0 si non leader)
--     "mur":           numeric,
--     "boss":          boolean, -- true ssi bonus_details.Bonus_Champion > 0
--     "rang_ligue":    int,     -- classement croise toutes divisions de la ligue
--     "points_ligue":  numeric  -- formule simplifiee v1, voir resolve_league_wide_ranks
--   }
-- ============================================================
create table division_classement_live (
    league_code  text not null references leagues(code),
    season       integer not null,
    division     integer not null,
    game_week    integer not null,
    data         jsonb not null,
    is_live      boolean not null default false,
    updated_at   timestamptz not null default now(),
    primary key (league_code, season, division)
);

-- ============================================================
-- Super classement general (fusion des ligues, bonus generaux inclus) --
-- remplace Classement_General/Super_Classement_General_*.json
-- ============================================================
create table super_classement (
    season         integer not null,
    user_id        text not null,
    points         numeric not null default 0,
    bonus_details  jsonb not null default '{}'::jsonb,
    updated_at     timestamptz not null default now(),
    primary key (season, user_id)
);

-- ============================================================
-- Config des 15 bonus generaux (admin-editable) -- remplace
-- Classement_General/general_bonus_config.json. Ligne unique (id = 1).
-- ============================================================
create table general_bonus_config (
    id               integer primary key default 1,
    categories       jsonb not null,
    included_leagues jsonb not null,
    updated_at       timestamptz not null default now(),
    constraint singleton check (id = 1)
);

-- ============================================================
-- Comptes managers -- remplace Classement_General/manager_accounts.json
-- IMPORTANT : cette table ne doit JAMAIS etre lisible/ecrivable directement
-- par le role anon (RLS ci-dessous) -- tout passe par les fonctions RPC
-- verify_manager_password / set_manager_password, qui seules connaissent le
-- hash. Le site (supabase-js, cle anonyme) n'appelle que ces fonctions.
-- ============================================================
create table accounts (
    user_id        text primary key,
    password_hash  text not null,
    is_admin       boolean not null default false,
    updated_at     timestamptz not null default now()
);

alter table accounts enable row level security;
-- Aucune policy = aucun acces direct pour anon/authenticated. Seules les
-- fonctions security definer ci-dessous peuvent lire/ecrire cette table.

create extension if not exists pgcrypto;

-- search_path = public, extensions (pas juste public) -- sur Supabase,
-- pgcrypto s'installe par defaut dans le schema "extensions", pas "public"
-- (retour utilisateur 2026-08-23 : "function crypt(text, text) does not
-- exist" a l'execution -- l'extension existait bien, juste invisible pour
-- une fonction SECURITY DEFINER dont le search_path ne couvrait pas le bon
-- schema).
create or replace function verify_manager_password(p_user_id text, p_password text)
returns boolean
language sql
security definer
set search_path = public, extensions
as $$
    select exists (
        select 1 from accounts
        where user_id = p_user_id
          and password_hash = crypt(p_password, password_hash)
    );
$$;

create or replace function set_manager_password(p_user_id text, p_password text)
returns void
language sql
security definer
set search_path = public, extensions
as $$
    insert into accounts (user_id, password_hash)
    values (p_user_id, crypt(p_password, gen_salt('bf')))
    on conflict (user_id) do update
        set password_hash = excluded.password_hash, updated_at = now();
$$;

-- A faire cote Supabase (dashboard SQL editor), pas encore inclus ici :
--   grant execute on function verify_manager_password to anon, authenticated;
--   grant execute on function set_manager_password to anon, authenticated;
--   (+ policies RLS pour les autres tables : lecture publique sur
--   live_snapshots/super_classement/league_classement_archive/
--   division_classement_live, ecriture reservee au role service_role
--   utilise par scripts/live_job.py)
