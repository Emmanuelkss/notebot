"""
Agent minimal avec l'API Claude (tool use).

L'agent peut lister et lire les fichiers d'un dossier "notes/",
puis répondre à ta question en s'appuyant dessus.

Installation :
    pip install anthropic
    Variable d'environnement ANTHROPIC_API_KEY=sk-ant-...
    (PyCharm : Run > Edit Configurations > Environment variables)

Utilisation :
    python agent.py      puis écris tes questions dans la console
                         (tape "quitter" pour sortir)
"""

import sys
from pathlib import Path

import anthropic

MODELE = "claude-sonnet-5-5"
DOSSIER = Path("notes").resolve()
MAX_TOURS = 10


OUTILS = [
    {
        "name": "lister_fichiers",
        "description": "Liste les fichiers disponibles dans le dossier de notes.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "lire_fichier",
        "description": "Retourne le contenu texte d'un fichier du dossier de notes.",
        "input_schema": {
            "type": "object",
            "properties": {"nom": {"type": "string", "description": "Nom du fichier, ex. 'pointeurs.txt'"}},
            "required": ["nom"],
        },
    },
]


# 2. Le CODE qui exécute vraiment chaque outil (c'est nous qui l'écrivons).
def executer_outil(nom, arguments):
    if nom == "lister_fichiers":
        fichiers = [f.name for f in DOSSIER.iterdir() if f.is_file()]
        return "\n".join(fichiers) or "(dossier vide)"

    if nom == "lire_fichier":
        chemin = (DOSSIER / arguments["nom"]).resolve()
        if chemin.parent != DOSSIER:
            return "Erreur : accès refusé hors du dossier de notes."
        if not chemin.is_file():
            return f"Erreur : '{arguments['nom']}' n'existe pas."
        return chemin.read_text(encoding="utf-8")[:20000]

    return f"Erreur : outil inconnu '{nom}'."


# 3. La BOUCLE de l'agent : traite UN message de l'utilisateur
#    (avec autant d'appels d'outils que nécessaire).
def agent(messages, client):
    for tour in range(MAX_TOURS):
        reponse = client.messages.create(
            model=MODELE,
            max_tokens=2048,
            system="Tu aides un étudiant à réviser. Utilise ses notes avant de répondre.",
            tools=OUTILS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": reponse.content})

        if reponse.stop_reason != "tool_use":

            return "".join(b.text for b in reponse.content if b.type == "text")


        resultats = []
        for bloc in reponse.content:
            if bloc.type == "tool_use":
                print(f"  [outil] {bloc.name}({bloc.input})")
                resultats.append({
                    "type": "tool_result",
                    "tool_use_id": bloc.id,
                    "content": executer_outil(bloc.name, bloc.input),
                })
        messages.append({"role": "user", "content": resultats})

    return "Arrêt : trop d'étapes."


# 4. Le CHAT : on garde la même liste "messages" d'une question à l'autre,
#    donc le bot se souvient de toute la conversation.
if __name__ == "__main__":
    DOSSIER.mkdir(exist_ok=True)
    client = anthropic.Anthropic()
    messages = []

    print("NoteBot prêt ! Pose ta question (tape 'quitter' pour sortir).")
    while True:
        question = input("\nToi : ").strip()
        if question.lower() in ("quitter", "exit", "q"):
            print("À plus !")
            break
        if not question:
            continue

        debut = len(messages)
        messages.append({"role": "user", "content": question})
        try:
            print("\nNoteBot :", agent(messages, client))
        except anthropic.APIError as erreur:
            del messages[debut:]
            print("\nErreur de l'API :", erreur)