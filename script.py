import os
import re
import threading
from pathlib import Path

import anthropic

MODELE = os.environ.get("NOTEBOT_MODELE", "claude-sonnet-5-5")
DOSSIER = Path(os.environ.get("NOTEBOT_NOTES", Path(__file__).parent / "notes")).resolve()
MAX_TOURS = 10
MAX_TOKENS = 2048
MAX_QUESTIONS = 20
MAX_CARACTERES = 20000
MAX_RESULTATS = 50
EXTENSIONS = {".txt", ".md"}
QUIZ_QUESTIONS = 5
VERDICTS = {"correct": 1.0, "partiel": 0.5, "incorrect": 0.0}

SYSTEME = (
    "Tu es NoteBot, un assistant qui aide un étudiant à réviser. "
    "Avant de répondre, consulte ses notes avec les outils disponibles. "
    "Indique le fichier sur lequel tu t'appuies. "
    "Si l'information n'est pas dans ses notes, dis-le clairement avant de répondre avec tes connaissances. "
    "Réponds dans la langue de l'étudiant."
)

OUTILS = [
    {
        "name": "lister_fichiers",
        "description": "Liste les fichiers de notes disponibles (.txt et .md).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "lire_fichier",
        "description": "Retourne le contenu complet d'un fichier de notes.",
        "input_schema": {
            "type": "object",
            "properties": {"nom": {"type": "string", "description": "Nom du fichier, ex. 'pointeurs.txt'"}},
            "required": ["nom"],
        },
    },
    {
        "name": "chercher",
        "description": "Cherche un mot ou une expression dans toutes les notes et retourne les lignes trouvées avec leur fichier et numéro de ligne.",
        "input_schema": {
            "type": "object",
            "properties": {"texte": {"type": "string", "description": "Mot ou expression à chercher (insensible à la casse)"}},
            "required": ["texte"],
        },
    },
]


class TropDEtapes(RuntimeError):
    pass


def fichiers_notes():
    if not DOSSIER.is_dir():
        return []
    return sorted(f for f in DOSSIER.iterdir() if f.is_file() and f.suffix.lower() in EXTENSIONS)


def lister_fichiers():
    noms = [f.name for f in fichiers_notes()]
    return "\n".join(noms) if noms else "Aucune note disponible."


def lire_fichier(nom):
    chemin = (DOSSIER / nom).resolve()
    if chemin.parent != DOSSIER or chemin.suffix.lower() not in EXTENSIONS:
        return "Erreur : fichier non autorisé."
    if not chemin.is_file():
        return f"Erreur : '{nom}' n'existe pas."
    texte = chemin.read_text(encoding="utf-8", errors="replace")
    if len(texte) > MAX_CARACTERES:
        return texte[:MAX_CARACTERES] + "\n[... contenu tronqué]"
    return texte


def chercher(texte):
    cible = texte.strip().lower()
    if not cible:
        return "Erreur : texte de recherche vide."
    resultats = []
    for fichier in fichiers_notes():
        lignes = fichier.read_text(encoding="utf-8", errors="replace").splitlines()
        for numero, ligne in enumerate(lignes, start=1):
            if cible in ligne.lower():
                resultats.append(f"{fichier.name}:{numero}: {ligne.strip()}")
                if len(resultats) >= MAX_RESULTATS:
                    return "\n".join(resultats) + f"\n[... limité aux {MAX_RESULTATS} premiers résultats]"
    return "\n".join(resultats) if resultats else f"Aucun résultat pour '{texte}'."


ACTIONS = {
    "lister_fichiers": lister_fichiers,
    "lire_fichier": lire_fichier,
    "chercher": chercher,
}


def executer_outil(nom, arguments):
    action = ACTIONS.get(nom)
    if action is None:
        return f"Erreur : outil inconnu '{nom}'."
    try:
        return action(**arguments)
    except (TypeError, OSError) as erreur:
        return f"Erreur : {erreur}"


def repondre(messages, client):
    outils_utilises = []
    for _ in range(MAX_TOURS):
        reponse = client.messages.create(
            model=MODELE,
            max_tokens=MAX_TOKENS,
            system=SYSTEME,
            tools=OUTILS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": reponse.content})

        if reponse.stop_reason != "tool_use":
            texte = "".join(bloc.text for bloc in reponse.content if bloc.type == "text")
            return texte, outils_utilises

        resultats = []
        for bloc in reponse.content:
            if bloc.type == "tool_use":
                outils_utilises.append({"nom": bloc.name, "arguments": bloc.input})
                resultats.append({
                    "type": "tool_result",
                    "tool_use_id": bloc.id,
                    "content": executer_outil(bloc.name, bloc.input),
                })
        messages.append({"role": "user", "content": resultats})

    raise TropDEtapes(f"Aucune réponse finale après {MAX_TOURS} étapes.")


class Conversation:
    def __init__(self, client):
        self.client = client
        self.messages = []
        self.verrou = threading.Lock()

    def envoyer(self, question):
        with self.verrou:
            self._limiter_historique()
            debut = len(self.messages)
            self.messages.append({"role": "user", "content": question})
            try:
                return repondre(self.messages, self.client)
            except Exception:
                del self.messages[debut:]
                raise

    def reinitialiser(self):
        with self.verrou:
            self.messages.clear()

    def _limiter_historique(self):
        debuts = [i for i, m in enumerate(self.messages) if m["role"] == "user" and isinstance(m["content"], str)]
        if len(debuts) >= MAX_QUESTIONS:
            del self.messages[:debuts[len(debuts) - MAX_QUESTIONS + 1]]


# --------------------------------------------------------------------------
# Mode quiz : NoteBot pose des questions tirées des notes et corrige
# --------------------------------------------------------------------------

def demande_question(numero, total, sujet):
    portee = f"sur « {sujet} »" if sujet else "sur un sujet de mes notes de ton choix"
    return (
        f"Mode quiz, question {numero} sur {total}, {portee}. "
        "Consulte mes notes avec les outils, puis pose-moi UNE seule question courte "
        "dont la réponse se trouve dans mes notes. Ne donne pas la réponse, "
        "et ne répète pas une question déjà posée dans ce quiz."
    )


def demande_correction(reponse):
    return (
        f"Ma réponse : {reponse}\n"
        "Corrige-la en t'appuyant sur mes notes. Commence ta réponse EXACTEMENT par "
        "une de ces lignes : « Verdict : correct », « Verdict : partiel » ou "
        "« Verdict : incorrect ». Ensuite, donne la bonne réponse en deux ou trois "
        "phrases et cite le fichier."
    )


def lire_verdict(texte):
    """Retourne 'correct', 'partiel', 'incorrect', ou None si le verdict est absent."""
    trouve = re.search(r"verdict\s*:\s*\**\s*(correct|partiel|incorrect)", texte, re.IGNORECASE)
    return trouve.group(1).lower() if trouve else None


def resume_score(verdicts):
    notes = [VERDICTS[v] for v in verdicts if v in VERDICTS]
    if not notes:
        return "Quiz terminé : aucune réponse notée."
    points = sum(notes)
    total = len(notes)
    pourcentage = round(100 * points / total)
    affichage = f"{points:g}/{total}"
    if pourcentage == 100:
        bilan = "Parfait, tu maîtrises ce sujet !"
    elif pourcentage >= 60:
        bilan = "Bon travail. Relis les points marqués partiel ou incorrect."
    else:
        bilan = "Ce sujet mérite encore une révision. Relis tes notes puis refais un quiz."
    return f"Quiz terminé : {affichage} ({pourcentage} %). {bilan}"


def lancer_quiz(conversation, sujet="", nombre=QUIZ_QUESTIONS, demander=input, afficher=print):
    """Pose `nombre` questions tirées des notes, corrige chaque réponse et affiche le score.

    `demander` et `afficher` remplacent input/print (utile pour les tests ou une interface).
    Tape 'stop' pendant le quiz pour l'arrêter. Retourne la liste des verdicts.
    """
    verdicts = []
    afficher(f"\nQuiz de {nombre} questions{f' sur « {sujet} »' if sujet else ''}. Tape 'stop' pour arrêter.")
    for numero in range(1, nombre + 1):
        question, _ = conversation.envoyer(demande_question(numero, nombre, sujet))
        afficher(f"\nQuestion {numero}/{nombre} : {question}")
        try:
            reponse = demander("Ta réponse : ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if reponse.lower() in ("stop", "quitter", "q"):
            break
        if not reponse:
            reponse = "je ne sais pas"
        correction, _ = conversation.envoyer(demande_correction(reponse))
        verdict = lire_verdict(correction)
        verdicts.append(verdict)
        afficher(f"\nNoteBot : {correction}")
    afficher("\n" + resume_score(verdicts))
    return verdicts


def main():
    DOSSIER.mkdir(parents=True, exist_ok=True)
    conversation = Conversation(anthropic.Anthropic())
    print("NoteBot prêt ! Pose ta question ('quiz [sujet]' pour t'entraîner, "
          "'reset' pour recommencer, 'quitter' pour sortir).")

    while True:
        try:
            question = input("\nToi : ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if question.lower() in ("quitter", "exit", "q"):
            break
        if question.lower() == "reset":
            conversation.reinitialiser()
            print("Conversation effacée.")
            continue
        if not question:
            continue
        if question.lower() == "quiz" or question.lower().startswith("quiz "):
            try:
                lancer_quiz(conversation, question[4:].strip())
            except anthropic.APIError as erreur:
                print("\nErreur de l'API :", erreur)
            except TropDEtapes as erreur:
                print("\nErreur :", erreur)
            continue

        try:
            texte, outils = conversation.envoyer(question)
        except anthropic.APIError as erreur:
            print("\nErreur de l'API :", erreur)
            continue
        except TropDEtapes as erreur:
            print("\nErreur :", erreur)
            continue

        for outil in outils:
            print(f"  [outil] {outil['nom']}({outil['arguments']})")
        print("\nNoteBot :", texte)

    print("À plus !")


if __name__ == "__main__":
    main()