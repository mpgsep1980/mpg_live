"""
Config de scoring/bonus effective d'une ligue -- fonctions pures d'un dict
`league` deja charge (voir scripts/live_job.py::get_all_leagues, qui lit la
table Supabase `leagues` -- sa colonne `scoring` jsonb a la meme forme que
`league["scoring"]` cote mpg_app/core/league.py, donc ces fonctions sont
portees ici sans aucun changement de logique).

Port partiel de mpg_app/core/league.py : uniquement get_scoring_config,
get_internal_bonus_config, get_match_bonus_config, DEFAULT_MATCH_BONUS_CONFIG,
current_real_season_start_year. Pas de load_leagues/save_leagues/
get_all_leagues (I/O fichier) -- remplaces par la version Supabase deja
presente dans scripts/live_job.py.
"""
from datetime import datetime

from core.scoring import DEFAULT_SCORING_CONFIG
from core.internal_bonus import DEFAULT_INTERNAL_BONUS_CONFIG


def current_real_season_start_year() -> int:
    """Annee de debut de la saison reelle actuellement suivie (ex. 2026 pour
    "2026-2027") -- regle calendaire standard "la saison commence en aout",
    identique a mpg_app/core/league.py (meme fonction, meme regle -- a NE
    JAMAIS diverger). Sert a nommer la saison du Super Classement
    (core/live_projection.py::compute_super_classement) plutot que le
    compteur de saison MPG d'UNE ligue (league["seasonSearch"]) : deux
    ligues suivant la MEME saison calendaire reelle peuvent avoir des
    compteurs MPG differents (ex. Ligue_2_EKT=21, Liga_Tapas=22 en meme
    temps, retour utilisateur 2026-08-23) -- une saison calendaire de foot
    peut regrouper plusieurs saisons MPG."""
    now = datetime.now()
    return now.year if now.month >= 8 else now.year - 1


def get_scoring_config(league: dict) -> dict:
    """Config de scoring effective d'une ligue : valeurs par défaut (DEFAULT_SCORING_CONFIG)
    surchargées par league["scoring"] si présent (édité depuis le mode admin)."""
    cfg = dict(DEFAULT_SCORING_CONFIG)
    cfg.update(league.get("scoring", {}))
    return cfg


def get_internal_bonus_config(league: dict) -> dict:
    """Config des bonus internes (Pichichi, Le Mur, Bonus_Champion/Podium/Boss_Saison)
    d'une ligue : valeurs par défaut surchargées par league["scoring"]["internalBonuses"]."""
    cfg = dict(DEFAULT_INTERNAL_BONUS_CONFIG)
    cfg.update(league.get("scoring", {}).get("internalBonuses", {}))
    return cfg


# Retour utilisateur 2026-08-20 : decision LDC ("lecture stricte -- seuls les
# points de match pondérés comptent, aucun autre bonus") -- ces champs
# entraient tous SANS AUCUN FILTRE DE LIGUE dans la formule "points" avant ce
# correctif cote mpg_app. Piloté par la config plutôt qu'un test en dur sur le
# nom de la ligue -- même mécanisme que DEFAULT_INTERNAL_BONUS_CONFIG.
DEFAULT_MATCH_BONUS_CONFIG = {
    "enabled": {
        "cleanSheet": True, "manita": True, "on_fire": True,
        "grotaldo": True, "precious": True,
    },
}


def get_match_bonus_config(league: dict) -> dict:
    """Config des bonus de MATCH (cleanSheet/manita/on_fire/grotaldo/Precious)
    d'une ligue : valeurs par défaut surchargées par league["scoring"]["matchBonuses"]."""
    cfg = dict(DEFAULT_MATCH_BONUS_CONFIG)
    cfg.update(league.get("scoring", {}).get("matchBonuses", {}))
    return cfg


def league_from_supabase_row(row: dict) -> dict:
    """Mappe une ligne brute de la table Supabase `leagues` (colonnes
    snake_case) vers le dict `league` camelCase attendu par ce module et par
    core/live_projection.py -- extrait de scripts/live_job.py::get_all_leagues
    (retour utilisateur 2026-08-26 : simulate_api/app.py a besoin de la MEME
    forme pour resoudre le classement d'UNE ligue via son short_id, sans
    dupliquer le mapping une deuxieme fois)."""
    return {
        "nom": row["nom"],
        "code": row["code"],
        "seasonSearch": row["season_search"],
        "seasonStart": row["season_start"],
        "championshipId": row["championship_id"],
        "playersNumber": row["players_number"],
        "playersPerDivision": row["players_per_division"],
        "poolGameweeks": row["pool_gameweeks"],
        "Div_A_Gameweeks": row["div_a_gameweeks"],
        "scoring": row.get("scoring") or {},
    }
