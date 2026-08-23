"""
Bonus "generaux" (inter-ligues) appliques au Super Classement -- fusion de
toutes les ligues par manager (cf. core/live_projection.py::
compute_super_classement).

Port PARTIEL de mpg_app/core/general_bonus.py (retour utilisateur
2026-08-23, "on y va etape par etape") -- apply_general_bonuses et
DEFAULT_GENERAL_BONUS_CATEGORIES sont copies VERBATIM (memes 16 entrees,
memes points/directions) : la fonction degrade deja proprement toute seule
si la cle source d'une categorie est absente de tous les managers (aucune
valeur trouvee -> extreme=None -> bonus_details[name]=0 pour tout le monde,
jamais d'erreur) -- inutile de retirer les categories dont la source
manque encore, elles s'activeront automatiquement le jour ou leur champ
sera ajoute au schema.

PAS encore portes : PinataScore/PINATA_DTC_WEIGHTS/compute_pinata_score
(La Piñata a besoin de 8 compteurs *_DTC, aucune source Supabase en v1),
Precious_Count (Gollum, meme raison que Precious ailleurs dans ce projet),
Bonus_Second/Bonus_Dernier cumules sur la saison (Poulidor/La Chèvre --
ces deux bonus internes ne sont pas encore accumules a travers les
journees dans league_classement_archive). Ces 4 categories restent dans
DEFAULT_GENERAL_BONUS_CATEGORIES (comportement identique a mpg_app) mais
resteront a 0 pour tout le monde tant que leurs champs sources ne sont pas
alimentes -- LIMITE CONNUE v1, documentee plutot que masquee.

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
