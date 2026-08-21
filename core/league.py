"""
Config de scoring/bonus effective d'une ligue -- fonctions pures d'un dict
`league` deja charge (voir scripts/live_job.py::get_all_leagues, qui lit la
table Supabase `leagues` -- sa colonne `scoring` jsonb a la meme forme que
`league["scoring"]` cote mpg_app/core/league.py, donc ces fonctions sont
portees ici sans aucun changement de logique).

Port partiel de mpg_app/core/league.py : uniquement get_scoring_config,
get_internal_bonus_config, get_match_bonus_config, DEFAULT_MATCH_BONUS_CONFIG.
Pas de load_leagues/save_leagues/get_all_leagues (I/O fichier) -- remplaces
par la version Supabase deja presente dans scripts/live_job.py.
"""
from core.scoring import DEFAULT_SCORING_CONFIG
from core.internal_bonus import DEFAULT_INTERNAL_BONUS_CONFIG


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
