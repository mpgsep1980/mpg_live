"""
Bonus "generaux" (inter-ligues) appliques au Super Classement -- fusion de
toutes les ligues par manager (cf. core/live_projection.py::
compute_super_classement).

Port de mpg_app/core/general_bonus.py -- apply_general_bonuses et
DEFAULT_GENERAL_BONUS_CATEGORIES copies VERBATIM (memes 16 entrees, memes
points/directions). Les 16 categories sont maintenant TOUTES alimentees
par une source reelle (retour utilisateur 2026-08-23 → 2026-08-24, "on y
va etape par etape" puis "vas-y" -- Boss_Saison/Bonus_Second/Bonus_Dernier/
Precious_Count/*_DTC portes un par un) -- plusieurs valent volontairement
0 point par regle officielle MPG (Winner, Pétard Mouillé, Passoire, FFL,
Macroniste, Harry Maguire Challenge, La Chèvre), pas par source manquante.
La fonction degrade quand meme proprement si une cle venait a manquer un
jour (extreme=None -> bonus_details[name]=0 pour tout le monde, jamais
d'erreur).

Poulidor/La Chèvre ACTIVES depuis le retour utilisateur 2026-08-24 ("vas-y",
comparaison contre Super_Classement_General_V2.ipynb) : Bonus_Second/
Bonus_Dernier etaient deja calcules par core.internal_bonus.
compute_internal_bonuses, simplement jamais surfaces jusqu'a
core/archive.py::_stats_from_row -- meme cle brute, aucune nouvelle
logique.

La Piñata ACTIVE depuis le retour utilisateur 2026-08-24 ("La Pinata est
un cumul du nombre de coups/bonus subis par un meme joueur, ratio 1 pour 1
sauf Valise a Nanard qui vaut 3 -- en theorie c'est documente") --
PINATA_DTC_WEIGHTS et compute_pinata_score portes verbatim de mpg_app.
Les 8 compteurs *_DTC eux-memes sont reconstruits par core/archive.py
depuis les matchs bruts (meme logique que mpg_app/backfill_historical_
season.py::bonuses_subis_from_match -- lit match[cote]["bonuses"], compte
1 par coup SUBI par cote adverse, plafonne a 1/match/coup) plutot que lus
d'un pipeline "Parsed" legacy sans equivalent Supabase.

Config editable non portee ici (load/save_general_bonus_config lisaient un
fichier JSON local cote mpg_app) -- DEFAULT_GENERAL_BONUS_CATEGORIES sert
directement de config, pas encore stockee/editable via general_bonus_config
(table Supabase deja presente dans le schema, pas encore utilisee)."""

DEFAULT_GENERAL_BONUS_CATEGORIES = [
    {"name": "Sulfateuse", "key": "score+", "direction": "max", "points": 10, "require_positive": False, "enabled": True},
    {"name": "Rideau de Fer", "key": "score-", "direction": "min", "points": 10, "require_positive": False, "enabled": True},
    {"name": "Winner", "key": "victory", "direction": "max", "points": 0, "require_positive": False, "enabled": True},
    {"name": "La Piñata", "key": "PinataScore", "direction": "max", "points": 10, "require_positive": True, "enabled": True},
    {"name": "En feu", "key": "on_fire", "direction": "max", "points": 10, "require_positive": False, "enabled": True},
    {"name": "Gollum", "key": "Precious_Count", "direction": "max", "points": 10, "require_positive": False, "enabled": True},
    {"name": "Pétard Mouillé", "key": "score+", "direction": "min", "points": 0, "require_positive": True, "enabled": True},
    {"name": "Passoire", "key": "score-", "direction": "max", "points": 0, "require_positive": False, "enabled": True},
    {"name": "L'Araignée", "key": "cleanSheet", "direction": "max", "points": 10, "require_positive": False, "enabled": True},
    {"name": "High Five", "key": "manita", "direction": "max", "points": 10, "require_positive": False, "enabled": True},
    {"name": "FFL", "key": "defeat", "direction": "max", "points": 0, "require_positive": False, "enabled": True},
    {"name": "Macroniste", "key": "draw", "direction": "max", "points": 0, "require_positive": False, "enabled": True},
    {"name": "King Grotaldo", "key": "grotaldo", "direction": "max", "points": -10, "require_positive": True, "enabled": True},
    {"name": "Harry Maguire Challenge", "key": "owngoals", "direction": "max", "points": 0, "require_positive": False, "enabled": True},
    {"name": "Poulidor", "key": "Bonus_Second", "direction": "max", "points": 5, "require_positive": False, "enabled": True},
    {"name": "La Chèvre", "key": "Bonus_Dernier", "direction": "max", "points": 0, "require_positive": False, "enabled": True},
]


# Port verbatim de mpg_app/core/general_bonus.py -- ponderation des 8
# compteurs *_DTC (coups SUBIS, cf. core/archive.py::_dtc_counts_from_matches
# pour leur reconstruction depuis les matchs bruts). Valise a Nanard compte
# triple (retour utilisateur 2026-08-24), les 7 autres comptent 1 pour 1.
PINATA_DTC_WEIGHTS = {
    "zahia_DTC": 1,
    "mcdo_DTC": 1,
    "suarez_DTC": 1,
    "cheatCode_DTC": 1,
    "nanard_DTC": 3,      # Valise a Nanard -- compte triple
    "tontonPat_DTC": 1,
    "mirror_DTC": 1,
    "QuatDecat_DTC": 1,
}


def compute_pinata_score(stats: dict) -> float:
    """Somme ponderee des 8 compteurs *_DTC -- a appeler pour CHAQUE manager
    du Super Classement fusionne, AVANT apply_general_bonuses (qui lit
    ensuite stats["PinataScore"] comme n'importe quelle autre cle)."""
    return sum(stats.get(key, 0) * weight for key, weight in PINATA_DTC_WEIGHTS.items())


def apply_general_bonuses(classement: list, categories: list[dict] | None = None) -> list:
    """`classement` : liste [user_id, stats_dict]. Mute et retourne
    `classement` -- ajoute/incremente stats_dict["points"] et remplit
    stats_dict["bonus_details"][category_name] pour chaque categorie
    active. Port verbatim de mpg_app/core/general_bonus.py (meme logique,
    meme signature)."""
    cats = [c for c in (categories or DEFAULT_GENERAL_BONUS_CATEGORIES) if c.get("enabled", True)]

    extremes: dict[str, float | None] = {}
    for cat in cats:
        values = [
            player[1].get(cat["key"]) for player in classement
            if player[1].get(cat["key"]) is not None
        ]
        if not values:
            extremes[cat["name"]] = None
        elif cat["direction"] == "min":
            extremes[cat["name"]] = min(values)
        else:
            extremes[cat["name"]] = max(values)

    for player in classement:
        stats = player[1]
        stats.setdefault("points", 0)
        stats.setdefault("bonus_details", {})

        for cat in cats:
            name = cat["name"]
            extreme = extremes[name]
            value = stats.get(cat["key"])

            if cat.get("require_positive") and (extreme is None or extreme <= 0):
                stats["bonus_details"][name] = 0
                continue

            if value is not None and value == extreme:
                stats["points"] += cat["points"]
                stats["bonus_details"][name] = cat["points"]
            else:
                stats["bonus_details"][name] = 0

    return classement
