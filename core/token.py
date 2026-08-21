"""
Token API MPG -- lu depuis la variable d'environnement MPG_TOKEN (secret
GitHub Actions, cf. .github/workflows/live_job.yml), jamais stocke dans un
fichier ni commite. Contrairement a la version locale (mpg_app/core/token.py,
qui lit config/token.json sur disque), ce repo tourne uniquement en CI/CD --
un fichier local n'aurait pas de sens ici.
"""
import base64
import json
import os


def decode_token_owner_id(token: str) -> str | None:
    """Decode le payload du JWT MPG (sans verifier la signature) pour en extraire
    l'id du proprietaire du token."""
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)  # re-ajoute le padding base64
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return payload.get("id")
    except (IndexError, ValueError, json.JSONDecodeError):
        return None


def load_token() -> str:
    token = os.environ.get("MPG_TOKEN", "")
    if not token:
        raise RuntimeError("Variable d'environnement MPG_TOKEN absente ou vide.")
    return token
