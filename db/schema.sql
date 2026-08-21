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
    players_per_division  integer default 8,
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

create or replace function verify_manager_password(p_user_id text, p_password text)
returns boolean
language sql
security definer
set search_path = public
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
set search_path = public
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
--   live_snapshots/super_classement/league_classement_archive, ecriture
--   reservee au role service_role utilise par scripts/live_job.py)
