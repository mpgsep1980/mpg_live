"""
Multi Boss (bonus inter-ligues, Super Classement) : un manager champion de
poule/division dans au moins 3 des 5 championnats reels (Ligue_des_Champignons
exclue) cumule des points selon un bareme par division -- port de
mpg_app/core/multi_boss.py (bareme MULTI_BOSS_POINTS_BY_DIVISION identique,
"poules" vaut 5 pts, autant qu'une D8/D9).

Contrairement a mpg_app (qui a acces a la vraie division remportee via
Champion_Division), le schema Supabase actuel de mpg_live n'archive que la
valeur BRUTE legacy de Boss_Saison (cf. core/archive.py::_stats_from_row,
core/live_projection.py::compute_super_classement) -- le meme flat 2/4 pts
que sommait a tort Super_Classement_General_V2.ipynb cellule 6 avant son
propre correctif (retour utilisateur 2026-08-25 : "il me semble qu'on avait
change le bareme a 5 points minimum"). division_won_from_boss_saison()
reconstruit la division/poule remportee a partir de cette valeur brute --
deterministe, chaque tableau boss_bonus (cf. MPG_Ligue_Camembert_Test_2025.
ipynb cellules 15/16 cote mpg_app, meme source que le legacy) est injectif
(valeurs toutes distinctes) par taille de ligue, et savoir si la saison MPG
concernee est la phase de poules (season == season_start) ou une phase de
division leve toute ambiguite entre les deux tables.
"""

MIN_CHAMPIONSHIPS = 3

# division remportee -> points, par nombre total de divisions de la ligue
# cette saison-la. "poules" : titre remporte pendant la phase de poules,
# avant la repartition en divisions.
MULTI_BOSS_POINTS_BY_DIVISION = {
    9: {"poules": 5, 1: 10, 2: 9, 3: 9, 4: 7, 5: 7, 6: 6, 7: 6, 8: 5, 9: 5},
}

# Tables legacy (identiques a MPG_Ligue_Camembert_Test_2025.ipynb cellules
# 15/16 cote mpg_app) -- valeur Boss_Saison brute attribuee au champion de
# chaque division, selon la taille de ligue.
BOSS_BONUS_TABLES_BY_SIZE = {
    48: {1: 10, 2: 8, 3: 7, 4: 6, 5: 5, 6: 4},
    72: {1: 10, 2: 9, 3: 8, 4: 7, 5: 6, 6: 5, 7: 4, 8: 3, 9: 2},
    96: {1: 10, 2: 9.5, 3: 9, 4: 8.5, 5: 8, 6: 7.5, 7: 7, 8: 6, 9: 5, 10: 4, 11: 3, 12: 2},
}
POULE_FLAT_VALUE_BY_SIZE = {48: 4, 72: 2, 96: 2}
BOSS_BONUS_INVERSE_BY_SIZE = {
    size: {v: d for d, v in table.items()} for size, table in BOSS_BONUS_TABLES_BY_SIZE.items()
}


def division_won_from_boss_saison(players_number: int | None, is_poule_season: bool, value: float) -> str | int | None:
    """Reconstruit ("poules" ou division_number) a partir de la valeur
    Boss_Saison brute archivee -- None si taille de ligue inconnue, ou si la
    valeur ne correspond a rien dans la table (ligue non couverte, donnee
    corrompue)."""
    if players_number not in BOSS_BONUS_TABLES_BY_SIZE:
        return None
    if is_poule_season:
        return "poules" if value == POULE_FLAT_VALUE_BY_SIZE.get(players_number) else None
    return BOSS_BONUS_INVERSE_BY_SIZE[players_number].get(value)


def multi_boss_points_for(players_number: int | None, division_won, tables: dict | None = None) -> float | None:
    """Points reels au bareme pour une division/poule remportee -- None si
    taille de ligue ou bareme non couvert (aucune extrapolation, cf.
    docstring module et mpg_app/core/multi_boss.py)."""
    if players_number is None:
        return None
    tables = tables if tables is not None else MULTI_BOSS_POINTS_BY_DIVISION
    table = tables.get(players_number // 8)
    if table is None:
        return None
    return table.get(division_won)
