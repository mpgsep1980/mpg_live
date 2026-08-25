"""
Automatise le declenchement de live_watch.py -- consulte le calendrier reel
(core.api.get_nearest_game_weeks) pour savoir QUAND chaque championnat suivi
demarre sa prochaine journee, et lance live_watch.py automatiquement des que
l'heure de coup d'envoi est atteinte, sans intervention manuelle (retour
utilisateur 2026-08-14 : "dans l'optique d'une automatisation totale... si on
cree une partie de script qui consulte les calendriers et decide donc de
lancer le script a l'heure prevue... je pense que cette API aidera").

Usage :
    python live_scheduler.py
    python live_scheduler.py --ligues Ligue_2_EKT,Rosbeef_League
    python live_scheduler.py --poll-interval 300 --watch-interval 60

Suit par defaut TOUTES les ligues -- le tri se fait deja plus bas dans la
boucle via le calendrier reel (currentGameWeek/nextGameWeek) : une ligue sans
mercato termine n'a ni l'un ni l'autre et est silencieusement ignoree ce
tour-la. (Historique : jusqu'au 2026-08-15 le filtre par defaut se basait sur
un classement archive existant pour la saison calendaire courante -- proxy
casse pour la toute premiere journee JAMAIS jouee d'une ligue, ex. Liga_Tapas
J1 le 2026-08-15 : mercato bien termine et match reellement en cours, mais
aucun classement encore archive donc ligue exclue du suivi automatique.)

Garde une trace de la derniere journee LANCEE par ligue dans
live_scheduler_state.json (persiste sur disque) pour survivre a un
redemarrage du scheduler sans relancer inutilement un live_watch.py deja en
cours pour la meme journee.

LIMITE CONNUE : si le scheduler lui-meme redemarre (ex. crash, machine
reboot) APRES avoir deja lance un live_watch.py, il perd la reference au
processus (dict `processes` en memoire uniquement) -- il ne pourra pas le
terminer proprement quand la journee suivante demarrera, un live_watch.py
"orphelin" continuera de tourner en parallele du nouveau. Sans consequence
sur la justesse des donnees (chaque live_watch.py ecrit son propre fichier
gw{N}.json, ne se marchent pas dessus), juste un peu de CPU/reseau
gaspille -- a purger manuellement si besoin (Gestionnaire des taches).
"""
import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from core.api import get_championship_ids, get_nearest_game_weeks
from core.league import get_all_leagues
from live_watch import SNAPSHOT_DIR

STATE_PATH = Path(__file__).parent / "live_scheduler_state.json"
LIVE_WATCH_PATH = Path(__file__).parent / "live_watch.py"

# Plan avant/apres (retour utilisateur 2026-08-17) : marge au-dessus des ~7h
# de revision des notes MPG post-match (confirme par l'utilisateur), pour
# etre certain que l'instantane "apres" capture des notes vraiment stables.
APRES_DELAY_HOURS = 8


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _avant_snapshot_path(short_id: str, season: int, game_week: int) -> Path:
    return SNAPSHOT_DIR / f"{short_id}_{season}_gw{game_week}_avant.json"


def _apres_snapshot_path(short_id: str, season: int, game_week: int) -> Path:
    return SNAPSHOT_DIR / f"{short_id}_{season}_gw{game_week}_apres.json"


def _apres_due(short_id: str, season: int, game_week: int) -> bool:
    """True si l'instantane "avant" (fige par live_watch.py des que tous les
    vrais matchs de la journee sont termines, cf. sa docstring) existe pour
    cette journee, qu'aucun "apres" n'a encore ete produit, et qu'au moins
    APRES_DELAY_HOURS se sont ecoulees depuis sa capture -- plan avant/apres
    (retour utilisateur 2026-08-17)."""
    avant_path = _avant_snapshot_path(short_id, season, game_week)
    apres_path = _apres_snapshot_path(short_id, season, game_week)
    if apres_path.exists() or not avant_path.exists():
        return False
    try:
        captured_at = parse_iso(json.loads(avant_path.read_text(encoding="utf-8"))["capturedAt"])
    except Exception:
        return False
    return datetime.now(timezone.utc) >= captured_at + timedelta(hours=APRES_DELAY_HOURS)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ligues", default=None, help="Noms de ligues separes par des virgules -- defaut : toutes les ligues (le calendrier reel tri ensuite ce qui est reellement suivable)")
    parser.add_argument("--poll-interval", type=int, default=600, help="Secondes entre deux consultations du calendrier (defaut 600 = 10 min)")
    parser.add_argument("--watch-interval", type=int, default=60, help="Passe tel quel a live_watch.py --interval")
    parser.add_argument("--once", action="store_true", help="Un seul passage (verifie le calendrier, lance si besoin) puis quitte -- pour tester sans boucler")
    args = parser.parse_args()

    all_leagues = get_all_leagues()
    if args.ligues:
        wanted = set(args.ligues.split(","))
        leagues = [l for l in all_leagues if l["nom"] in wanted]
    else:
        leagues = all_leagues

    if not leagues:
        raise SystemExit("Aucune ligue a suivre (--ligues ne correspond a rien).")

    print(f"Scheduler demarre -- ligues suivies : {', '.join(l['nom'] for l in leagues)}")
    print(f"Calendrier reconsulte toutes les {args.poll_interval}s\n")

    state = load_state()  # {leagueName: {"launchedGameWeek": N, "startedAt": iso}}
    processes: dict[str, subprocess.Popen] = {}

    while True:
        try:
            championship_ids = get_championship_ids()
            calendars = get_nearest_game_weeks()
        except Exception as e:
            print(f"[{datetime.now():%H:%M:%S}] Erreur calendrier : {e} -- nouvel essai dans {args.poll_interval}s")
            time.sleep(args.poll_interval)
            continue

        now = datetime.now(timezone.utc)
        for league in leagues:
            name = league["nom"]
            # Chaque ligue est isolee dans son propre try/except -- une
            # journee peut s'etaler sur 4-5 jours reels (retour utilisateur
            # 2026-08-14), le scheduler doit donc tourner sans surveillance
            # sur plusieurs jours d'affilee. Avant ce correctif, une exception
            # inattendue sur UNE SEULE ligue (forme de donnees imprevue,
            # echec de subprocess.Popen...) faisait planter tout le
            # scheduler -- invisible sur un test de quelques minutes, presque
            # garanti d'arriver sur plusieurs jours sans personne pour
            # relancer a la main.
            try:
                # Plan avant/apres, volet "apres" (retour utilisateur
                # 2026-08-17) : independant de la resolution current_gw/
                # next_gw ci-dessous (qui peut deja pointer vers la journee
                # SUIVANTE une fois celle-ci finie) -- se base directement sur
                # state[name]["launchedGameWeek"], la derniere journee
                # effectivement suivie par live_watch.py pour cette ligue.
                league_state = state.get(name, {})
                launched_gw = league_state.get("launchedGameWeek")
                season_for_apres = league.get("seasonSearch")
                if launched_gw and season_for_apres and league_state.get("apresLaunchedGameWeek") != launched_gw:
                    if _apres_due(league["code"], season_for_apres, launched_gw):
                        print(f"[{datetime.now():%H:%M:%S}] {name} J{launched_gw} : instantane AVANT fige depuis "
                              f">= {APRES_DELAY_HOURS}h -- lancement du calcul APRES")
                        subprocess.Popen([
                            sys.executable, str(LIVE_WATCH_PATH),
                            "--ligue", name, "--gameweek", str(launched_gw), "--apres",
                        ])
                        state[name]["apresLaunchedGameWeek"] = launched_gw
                        save_state(state)

                champ_id = championship_ids.get(league["code"])
                if champ_id is None:
                    print(f"  {name} : championshipId introuvable sur le dashboard, ignoree ce tour.")
                    continue

                entry = calendars.get(str(champ_id), {})
                current_gw = entry.get("currentGameWeek")
                next_gw = entry.get("nextGameWeek")

                # currentGameWeek n'existe QUE pendant la fenetre reelle d'une
                # journee (absent avant coup d'envoi, cf. previousGameWeek/
                # nextGameWeek pour ce cas) -- c'est le signal officiel MPG
                # qu'une journee est en cours, prioritaire sur tout calcul de
                # date fait de notre cote. Trouve en observant le vrai
                # comportement de l'API le 2026-08-14 : live_scheduler ne
                # verifiait avant que nextGameWeek, qui bascule sur J3 des
                # que J2 demarre -- la fenetre de lancement de J2 etait donc
                # sautee silencieusement.
                if current_gw and current_gw.get("gameWeekNumber"):
                    start_date = now  # deja demarre, on ne l'utilise que pour l'affichage
                    game_week = current_gw["gameWeekNumber"]
                elif next_gw and next_gw.get("startDate") and next_gw.get("gameWeekNumber"):
                    start_date = parse_iso(next_gw["startDate"])
                    game_week = next_gw["gameWeekNumber"]
                else:
                    continue

                already_launched = state.get(name, {}).get("launchedGameWeek") == game_week
                # Sonde de sante (retour utilisateur 2026-08-23) : si live_watch.py
                # de CETTE ligue a ete lance PAR CE meme scheduler (donc on a un
                # vrai handle Popen -- differe du redemarrage du scheduler
                # lui-meme, cf. LIMITE CONNUE en tete de module, qui perd le
                # handle et n'est PAS concernee ici) et qu'il s'est arrete tout
                # seul (crash, exception non geree...), already_launched=True ne
                # doit plus bloquer la relance -- sans cette sonde, Liga_Tapas J2
                # est reste fige sur un instantane du 2026-08-20 pendant 3 jours
                # (live_watch.py mort, mais jamais reessaye faute de transition
                # de journee -- verifie via live_scheduler_state.json,
                # launchedGameWeek jamais mis a jour depuis son lancement initial).
                tracked_proc = processes.get(name)
                if already_launched and tracked_proc is not None and tracked_proc.poll() is not None:
                    print(f"[{datetime.now():%H:%M:%S}] {name} J{game_week} : live_watch.py "
                          f"(pid {tracked_proc.pid}) arrete de façon inattendue -- relance.")
                    already_launched = False

                if now >= start_date and not already_launched:
                    print(f"[{datetime.now():%H:%M:%S}] {name} J{game_week} demarre "
                          f"(coup d'envoi {start_date:%d/%m %H:%M} UTC) -- lancement de live_watch.py")
                    old_proc = processes.get(name)
                    if old_proc and old_proc.poll() is None:
                        print(f"  Arret de l'ancien live_watch.py de {name} (journee precedente).")
                        old_proc.terminate()
                    proc = subprocess.Popen([
                        sys.executable, str(LIVE_WATCH_PATH),
                        "--ligue", name, "--gameweek", str(game_week), "--interval", str(args.watch_interval),
                    ])
                    processes[name] = proc
                    state[name] = {"launchedGameWeek": game_week, "startedAt": datetime.now(timezone.utc).isoformat()}
                    save_state(state)
                elif now < start_date:
                    remaining = start_date - now
                    print(f"  {name} : J{game_week} prevue dans {remaining} ({start_date:%d/%m %H:%M} UTC)")
                else:
                    print(f"  {name} : J{game_week} deja lancee.")
            except Exception as e:
                print(f"  {name} : erreur inattendue ce tour ({e}) -- reessai au prochain sondage.")

        if args.once:
            break
        time.sleep(args.poll_interval)


if __name__ == "__main__":
    main()
