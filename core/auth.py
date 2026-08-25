"""
Comptes managers (mot de passe simple par manager) -- retour utilisateur
2026-08-12 : "Chaque joueur creera rapidement un compte, ça evitera les fuites
de bonus involontaire". Perimetre volontairement etroit : un compte sert
UNIQUEMENT a prouver "je suis bien le manager X" pour debloquer SES PROPRES
controles de bonus sur /manager?id=X (cf. admin_server.py) -- le reste du site
(scores, classements, compositions) reste public entre managers, comme avant.

Un membre de ADMIN_USER_IDS peut reinitialiser le mot de passe de n'importe
quel manager (mdp perdu) et acceder aux pages reservees (Simulateur/Comptes/
Barèmes/Token API) mais N'A PAS de passe-droit sur les controles de bonus des
autres -- explicitement demande par l'utilisateur ("je ne veux pas d'acces
aux bonus des autres"), verifie cote serveur par une comparaison stricte
session['userId'] == target_user_id, jamais par un statut admin.

Stockage : Classement_General/manager_accounts.json, {userId: {"passwordHash"}}
-- hash via werkzeug.security (scrypt, salage integre), jamais de mot de passe
en clair sur disque.
"""
import json
from pathlib import Path

from werkzeug.security import generate_password_hash, check_password_hash

from config import BASE_PATH

ACCOUNTS_PATH = BASE_PATH / "Classement_General" / "manager_accounts.json"

# Admin(s) -- reinitialisation de mot de passe (cf. docstring module) ET
# acces aux pages reservees (Simulateur/Comptes/Barèmes/Token API, retour
# utilisateur 2026-08-23 : "à garder pour le ou les admins que je désigne").
# Un set pour permettre plusieurs admins -- Sep (user_367647) est le seul a
# ce jour, ajouter un userId ici suffit pour en designer un autre.
ADMIN_USER_IDS = {"user_367647"}


def load_accounts() -> dict[str, dict]:
    if not ACCOUNTS_PATH.exists():
        return {}
    return json.loads(ACCOUNTS_PATH.read_text(encoding="utf-8"))


def save_accounts(accounts: dict[str, dict]) -> None:
    ACCOUNTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    ACCOUNTS_PATH.write_text(json.dumps(accounts, ensure_ascii=False, indent=2), encoding="utf-8")


def has_password(user_id: str) -> bool:
    return user_id in load_accounts()


def set_password(user_id: str, password: str) -> None:
    accounts = load_accounts()
    accounts[user_id] = {"passwordHash": generate_password_hash(password)}
    save_accounts(accounts)


def verify_password(user_id: str, password: str) -> bool:
    account = load_accounts().get(user_id)
    if not account:
        return False
    return check_password_hash(account["passwordHash"], password)


def clear_password(user_id: str) -> None:
    """Reinitialisation admin (mdp perdu) -- supprime le mot de passe existant,
    le manager en recree un a la prochaine connexion (meme flux que la
    premiere fois, pas de mdp temporaire a transmettre)."""
    accounts = load_accounts()
    if user_id in accounts:
        del accounts[user_id]
        save_accounts(accounts)
