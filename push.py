"""Commit tout et pousse sur GitHub en une seule commande.

    python push.py "Ajout du mode quiz"
    python push.py "Essai" --branch mon-idee     # pousse sur une autre branche

Ou depuis Python :

    from push import pousser_sur_github
    pousser_sur_github("Ajout du mode quiz")

La fonction ajoute tous les changements, fait le commit avec ton message et
pousse la branche sur `origin` (en la créant sur GitHub la première fois).
Elle utilise tes propres identifiants git : lance-la sur ton ordi, dans le dépôt.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

RACINE = Path(__file__).resolve().parent


class ErreurPush(RuntimeError):
    pass


def _git(*args: str, verifier: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=RACINE, capture_output=True, text=True)
    if verifier and proc.returncode != 0:
        raise ErreurPush(f"git {' '.join(args)} a échoué :\n{proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip()


def pousser_sur_github(message: str, branche: str | None = None, remote: str = "origin") -> str:
    """Ajoute, commit et pousse. Retourne le nom de la branche poussée."""
    if not message or not message.strip():
        raise ErreurPush('Donne un message de commit, ex. pousser_sur_github("Ajout du quiz").')
    _git("rev-parse", "--is-inside-work-tree")
    if remote not in _git("remote").split():
        raise ErreurPush(f"Aucun remote nommé {remote!r}. Ajoute-le avec : git remote add {remote} <url>")

    actuelle = _git("symbolic-ref", "--quiet", "--short", "HEAD", verifier=False)
    if not actuelle:
        raise ErreurPush("Tu n'es sur aucune branche. Lance : git switch <branche>")
    cible = branche or actuelle
    if branche and branche != actuelle:
        existe = _git("rev-parse", "--verify", "--quiet", branche, verifier=False)
        _git("switch", branche) if existe else _git("switch", "-c", branche)

    _git("add", "-A")
    if _git("status", "--porcelain"):
        _git("commit", "-m", message.strip())
        print(f"Commit : {message.strip()}")
    else:
        print("Rien de nouveau à commiter, je pousse les commits existants.")

    print(f"Push de {cible} vers {remote}...")
    _git("push", "-u", remote, cible)
    url = _git("remote", "get-url", remote).removesuffix(".git")
    if url.startswith("git@github.com:"):
        url = "https://github.com/" + url.split(":", 1)[1]
    print(f"Terminé : {url}/tree/{cible}")
    return cible


def main() -> None:
    ap = argparse.ArgumentParser(description="Commit tous les changements et pousse sur GitHub.")
    ap.add_argument("message", help="message du commit")
    ap.add_argument("--branch", help="branche où pousser (par défaut : la branche actuelle)")
    ap.add_argument("--remote", default="origin", help="remote git (par défaut : origin)")
    args = ap.parse_args()
    try:
        pousser_sur_github(args.message, branche=args.branch, remote=args.remote)
    except ErreurPush as erreur:
        sys.exit(f"Erreur : {erreur}")


if __name__ == "__main__":
    main()
