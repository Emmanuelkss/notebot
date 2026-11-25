import datetime as dt
import json
import logging
import os
import re
import threading
import unicodedata
from pathlib import Path

import anthropic

try:
    from pypdf import PdfReader
except ImportError:  # pypdf est optionnel : sans lui, les PDF sont listés mais pas lus
    PdfReader = None

# pypdf écrit des avertissements techniques dans la console pour les PDF imparfaits ;
# NoteBot gère déjà ces cas et affiche son propre message.
logging.getLogger("pypdf").setLevel(logging.ERROR)

MODELE = os.environ.get("NOTEBOT_MODELE", "claude-sonnet-5-5")
DOSSIER = Path(os.environ.get("NOTEBOT_NOTES", Path(__file__).parent / "notes")).resolve()
FICHES = Path(os.environ.get("NOTEBOT_FICHES", Path(__file__).parent / "fiches")).resolve()
CONVERSATIONS = Path(os.environ.get("NOTEBOT_CONVERSATIONS", Path(__file__).parent / "conversations")).resolve()
PROGRES = Path(os.environ.get("NOTEBOT_PROGRES", Path(__file__).parent / "progres.json")).resolve()
MAX_TOURS = 10
MAX_TOKENS = 2048
MAX_TOKENS_FICHE = 4096
MAX_QUESTIONS = 20
MAX_CARACTERES = 20000
MAX_RESULTATS = 50
EXTENSIONS = {".txt", ".md", ".pdf"}
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
        "description": "Liste les fichiers de notes disponibles (.txt, .md et .pdf).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "lire_fichier",
        "description": "Retourne le contenu complet d'un fichier de notes. Pour un PDF, le texte est découpé avec des marqueurs [Page N].",
        "input_schema": {
            "type": "object",
            "properties": {"nom": {"type": "string", "description": "Nom du fichier, ex. 'pointeurs.txt' ou 'chapitre2.pdf'"}},
            "required": ["nom"],
        },
    },
    {
        "name": "chercher",
        "description": "Cherche un mot ou une expression dans toutes les notes et retourne les lignes trouvées avec leur fichier et leur numéro de ligne (ou de page pour un PDF).",
        "input_schema": {
            "type": "object",
            "properties": {"texte": {"type": "string", "description": "Mot ou expression à chercher (insensible à la casse)"}},
            "required": ["texte"],
        },
    },
]


class TropDEtapes(RuntimeError):
    pass


class ErreurLecture(Exception):
    """Une note existe mais son texte ne peut pas être extrait (PDF protégé, corrompu, etc.)."""


def fichiers_notes():
    if not DOSSIER.is_dir():
        return []
    return sorted(f for f in DOSSIER.iterdir() if f.is_file() and f.suffix.lower() in EXTENSIONS)


# --------------------------------------------------------------------------
# Lecture des notes : texte brut ou PDF (avec cache, car extraire un PDF est lent)
# --------------------------------------------------------------------------

_cache_pdf = {}  # chemin -> (date de modification, [texte de chaque page])


def pages_pdf(chemin):
    """Retourne la liste du texte de chaque page d'un PDF. Le résultat est mis en cache
    tant que le fichier n'est pas modifié, pour que `chercher` reste rapide."""
    if PdfReader is None:
        raise ErreurLecture("lire les PDF demande le module pypdf. Installe-le avec : pip install pypdf")
    date = chemin.stat().st_mtime
    en_cache = _cache_pdf.get(chemin)
    if en_cache is None or en_cache[0] != date:
        try:
            lecteur = PdfReader(chemin)
            if lecteur.is_encrypted and not lecteur.decrypt(""):
                raise ErreurLecture(f"'{chemin.name}' est protégé par un mot de passe.")
            resultat = [(page.extract_text() or "").strip() for page in lecteur.pages]
        except ErreurLecture as erreur:
            resultat = erreur
        except Exception as erreur:  # pypdf lève des erreurs variées sur les PDF abîmés
            resultat = ErreurLecture(f"impossible de lire '{chemin.name}' ({erreur}).")
        # On garde aussi les échecs en cache pour ne pas relire un PDF abîmé à chaque recherche
        en_cache = _cache_pdf[chemin] = (date, resultat)
    if isinstance(en_cache[1], ErreurLecture):
        raise en_cache[1]
    return en_cache[1]


def sections(chemin):
    """Découpe une note en sections : une par page pour un PDF, une seule sinon.
    Retourne une liste de (numéro de page ou None, texte)."""
    if chemin.suffix.lower() == ".pdf":
        return list(enumerate(pages_pdf(chemin), start=1))
    return [(None, chemin.read_text(encoding="utf-8", errors="replace"))]


def texte_note(chemin):
    morceaux = sections(chemin)
    if chemin.suffix.lower() != ".pdf":
        return morceaux[0][1]
    if not any(texte for _, texte in morceaux):
        raise ErreurLecture(
            f"aucun texte trouvé dans '{chemin.name}'. C'est sans doute un PDF numérisé (des images) : "
            "il faudrait d'abord le passer dans un logiciel de reconnaissance de texte (OCR)."
        )
    return "\n\n".join(f"[Page {page}]\n{texte}" for page, texte in morceaux)


def lister_fichiers():
    noms = [f.name for f in fichiers_notes()]
    return "\n".join(noms) if noms else "Aucune note disponible."


def lire_fichier(nom):
    chemin = (DOSSIER / nom).resolve()
    if chemin.parent != DOSSIER or chemin.suffix.lower() not in EXTENSIONS:
        return "Erreur : fichier non autorisé."
    if not chemin.is_file():
        return f"Erreur : '{nom}' n'existe pas."
    texte = texte_note(chemin)
    if len(texte) > MAX_CARACTERES:
        return texte[:MAX_CARACTERES] + "\n[... contenu tronqué]"
    return texte


def chercher(texte):
    cible = texte.strip().lower()
    if not cible:
        return "Erreur : texte de recherche vide."
    resultats = []
    illisibles = []
    for fichier in fichiers_notes():
        try:
            morceaux = sections(fichier)
        except ErreurLecture:
            illisibles.append(fichier.name)
            continue
        for page, contenu in morceaux:
            for numero, ligne in enumerate(contenu.splitlines(), start=1):
                if cible in ligne.lower():
                    position = f"page {page}" if page else str(numero)
                    resultats.append(f"{fichier.name}:{position}: {ligne.strip()}")
                    if len(resultats) >= MAX_RESULTATS:
                        return "\n".join(resultats) + f"\n[... limité aux {MAX_RESULTATS} premiers résultats]"
    texte_final = "\n".join(resultats) if resultats else f"Aucun résultat pour '{texte}'."
    if illisibles:
        texte_final += f"\n[Fichiers non lus : {', '.join(illisibles)}]"
    return texte_final


# --------------------------------------------------------------------------
# Commandes rapides : 'notes' et 'cherche' fonctionnent sans appel à l'API
# --------------------------------------------------------------------------

def taille_lisible(octets):
    for unite, taille in (("Mo", 1024 ** 2), ("Ko", 1024)):
        if octets >= taille:
            return f"{octets / taille:.1f} {unite}".replace(".", ",")
    return f"{octets} o"


def resume_notes():
    """Liste les notes avec leur taille, leur date de modification et, pour un PDF, son nombre de pages."""
    fichiers = fichiers_notes()
    if not fichiers:
        return f"Aucune note pour l'instant. Ajoute des fichiers .txt, .md ou .pdf dans : {DOSSIER}"
    lignes = [f"{len(fichiers)} note{'s' if len(fichiers) > 1 else ''} dans {DOSSIER} :", ""]
    for chemin in fichiers:
        infos = chemin.stat()
        details = [taille_lisible(infos.st_size),
                   f"modifié le {dt.datetime.fromtimestamp(infos.st_mtime):%d/%m/%Y}"]
        if chemin.suffix.lower() == ".pdf":
            try:
                pages = pages_pdf(chemin)
                details.insert(0, f"{len(pages)} page{'s' if len(pages) > 1 else ''}")
                if not any(pages):
                    details.append("aucun texte (PDF numérisé ?)")
            except ErreurLecture as erreur:
                details.append(f"illisible : {erreur}")
        lignes.append(f"- {chemin.name} ({', '.join(details)})")
    return "\n".join(lignes)


def recherche_rapide(texte):
    """Lance l'outil `chercher` directement, et aide l'étudiant si rien n'est trouvé."""
    texte = texte.strip()
    if not texte:
        return "Précise ce que tu cherches, par exemple : cherche nullptr"
    resultat = chercher(texte)
    if resultat.startswith("Aucun résultat"):
        resultat += ("\nAstuce : 'cherche' trouve le texte exact. Essaie un mot plus court, "
                     "ou pose ta question sans 'cherche' devant pour que NoteBot t'aide.")
    return resultat


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
    except ErreurLecture as erreur:
        return f"Erreur : {erreur}"
    except (TypeError, OSError) as erreur:
        return f"Erreur : {erreur}"


# --------------------------------------------------------------------------
# Consommation : tokens utilisés depuis le lancement de NoteBot
# --------------------------------------------------------------------------

def prix_env(nom):
    """Lit un prix en dollars par million de tokens (ex. '3' ou '3,5'). None si absent ou invalide."""
    valeur = os.environ.get(nom, "").strip().replace(",", ".")
    try:
        prix = float(valeur)
    except ValueError:
        return None
    return prix if prix >= 0 else None


class Consommation:
    """Additionne les tokens de chaque appel à l'API pendant la séance."""

    def __init__(self):
        self.appels = 0
        self.entree = 0
        self.sortie = 0

    def ajouter(self, usage):
        self.appels += 1
        if usage is not None:
            self.entree += (getattr(usage, "input_tokens", 0) or 0)
            # Les tokens lus ou écrits en cache comptent aussi comme tokens d'entrée
            self.entree += (getattr(usage, "cache_read_input_tokens", 0) or 0)
            self.entree += (getattr(usage, "cache_creation_input_tokens", 0) or 0)
            self.sortie += (getattr(usage, "output_tokens", 0) or 0)

    def cout(self, prix_entree, prix_sortie):
        """Coût estimé en dollars, avec des prix en dollars par million de tokens."""
        return (self.entree * prix_entree + self.sortie * prix_sortie) / 1_000_000

    def resume(self, prix_entree=None, prix_sortie=None):
        if not self.appels:
            return "Aucun appel à l'API pour l'instant dans cette séance."
        nombre = lambda n: f"{n:,}".replace(",", " ")
        lignes = [
            f"Consommation de la séance ({MODELE}) :",
            f"- {self.appels} appel{'s' if self.appels > 1 else ''} à l'API",
            f"- {nombre(self.entree)} tokens envoyés (notes, historique, questions)",
            f"- {nombre(self.sortie)} tokens générés (réponses)",
        ]
        if prix_entree is not None and prix_sortie is not None:
            lignes.append(f"- Coût estimé : {self.cout(prix_entree, prix_sortie):.4f} $".replace(".", ","))
        else:
            lignes.append("Pour voir un coût estimé, indique les prix de ton modèle (en $ par million de tokens) "
                          "avec NOTEBOT_PRIX_ENTREE et NOTEBOT_PRIX_SORTIE.")
        return "\n".join(lignes)


CONSOMMATION = Consommation()


def repondre(messages, client, max_tokens=MAX_TOKENS):
    outils_utilises = []
    for _ in range(MAX_TOURS):
        reponse = client.messages.create(
            model=MODELE,
            max_tokens=max_tokens,
            system=SYSTEME,
            tools=OUTILS,
            messages=messages,
        )
        CONSOMMATION.ajouter(getattr(reponse, "usage", None))
        if reponse.stop_reason != "tool_use":
            # Réponse finale. Si elle a été coupée (max_tokens) pendant un appel d'outil, le
            # bloc tool_use n'a jamais de tool_result : le garder dans l'historique ferait
            # échouer toutes les questions suivantes. On ne conserve donc que le texte.
            textes = [bloc for bloc in reponse.content if bloc.type == "text"]
            texte = "".join(bloc.text for bloc in textes)
            if reponse.stop_reason == "max_tokens":
                texte = (texte.rstrip() + "\n\n[Réponse coupée : limite de longueur atteinte. "
                         "Demande la suite ou pose une question plus précise.]").lstrip()
            messages.append({"role": "assistant", "content": textes or texte or "(réponse vide)"})
            return texte, outils_utilises

        messages.append({"role": "assistant", "content": reponse.content})

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

    def echanges(self):
        """Retourne la conversation sous forme de [(message de l'étudiant, réponse finale de NoteBot)]."""
        with self.verrou:
            return echanges(self.messages)

    def reinitialiser(self):
        with self.verrou:
            self.messages.clear()

    def _limiter_historique(self):
        debuts = [i for i, m in enumerate(self.messages) if m["role"] == "user" and isinstance(m["content"], str)]
        if len(debuts) >= MAX_QUESTIONS:
            del self.messages[:debuts[len(debuts) - MAX_QUESTIONS + 1]]


# --------------------------------------------------------------------------
# Export : enregistre la conversation dans un fichier Markdown
# --------------------------------------------------------------------------

def texte_message(contenu):
    """Extrait le texte d'un message : une chaîne, ou une liste de blocs (objets de l'API ou dict)."""
    if isinstance(contenu, str):
        return contenu
    morceaux = []
    for bloc in contenu:
        if isinstance(bloc, dict):
            if bloc.get("type") == "text":
                morceaux.append(bloc.get("text", ""))
        elif getattr(bloc, "type", None) == "text":
            morceaux.append(bloc.text)
    return "".join(morceaux)


def echanges(messages):
    """Regroupe l'historique en (question, réponse). Les appels d'outils sont ignorés : pour chaque
    question, on garde seulement le dernier message de NoteBot, c'est-à-dire sa réponse finale."""
    resultat = []
    for message in messages:
        if message["role"] == "user" and isinstance(message["content"], str):
            resultat.append([message["content"], ""])
        elif message["role"] == "assistant" and resultat:
            resultat[-1][1] = texte_message(message["content"])
    return [(question, reponse) for question, reponse in resultat]


def question_affichee(question):
    """Remplace les consignes internes du quiz par un texte lisible dans l'export."""
    if question.startswith("Mode quiz, question "):
        numero = re.match(r"Mode quiz, question (\d+ sur \d+)", question)
        return f"*Quiz : question {numero.group(1)}*" if numero else "*Quiz*"
    if question.startswith("Ma réponse : "):
        return "Ma réponse : " + question[len("Ma réponse : "):].split("\n", 1)[0]
    return question


def conversation_markdown(liste, date=None):
    date = date or dt.datetime.now()
    lignes = [f"# Conversation NoteBot du {date:%d/%m/%Y à %H:%M}", ""]
    for question, reponse in liste:
        lignes += ["## Toi", "", question_affichee(question), "", "## NoteBot", "",
                   reponse.strip() or "*(pas de réponse)*", ""]
    return "\n".join(lignes)


def exporter_conversation(conversation, dossier=None, date=None):
    """Enregistre la conversation dans <dossier>/AAAA-MM-JJ_HHhMM.md.
    Retourne le chemin créé, ou None si la conversation est vide."""
    liste = conversation.echanges()
    if not liste:
        return None
    dossier = dossier or CONVERSATIONS
    date = date or dt.datetime.now()
    dossier.mkdir(parents=True, exist_ok=True)
    chemin = chemin_libre(f"{date:%Y-%m-%d_%Hh%M}", dossier)
    chemin.write_text(conversation_markdown(liste, date), encoding="utf-8")
    return chemin


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


def points_quiz(verdicts):
    """Retourne (points, nombre de réponses notées). Les verdicts absents ne comptent pas."""
    notes = [VERDICTS[v] for v in verdicts if v in VERDICTS]
    return sum(notes), len(notes)


def resume_score(verdicts):
    points, total = points_quiz(verdicts)
    if not total:
        return "Quiz terminé : aucune réponse notée."
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


# --------------------------------------------------------------------------
# Progression : chaque quiz est enregistré pour suivre ses points faibles
# --------------------------------------------------------------------------

SUJET_GENERAL = "sujets variés"
SEUIL_A_REVOIR = 60


def charger_progres(fichier=None):
    """Lit l'historique des quiz. Un fichier absent ou abîmé donne une liste vide."""
    fichier = fichier or PROGRES
    try:
        donnees = json.loads(fichier.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(donnees, list):
        return []
    return [d for d in donnees if isinstance(d, dict) and {"sujet", "points", "total"} <= d.keys()
            and isinstance(d["total"], int) and d["total"] > 0]


def enregistrer_quiz(sujet, verdicts, fichier=None, date=None):
    """Ajoute le résultat d'un quiz à l'historique. Un quiz sans réponse notée n'est pas gardé.
    Retourne True si le résultat a été enregistré."""
    fichier = fichier or PROGRES
    points, total = points_quiz(verdicts)
    if not total:
        return False
    historique = charger_progres(fichier)
    historique.append({
        "date": (date or dt.datetime.now()).isoformat(timespec="minutes"),
        "sujet": sujet.strip() or SUJET_GENERAL,
        "points": points,
        "total": total,
    })
    fichier.parent.mkdir(parents=True, exist_ok=True)
    fichier.write_text(json.dumps(historique, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def resume_progres(historique):
    """Tableau par sujet : nombre de quiz, dernier score, meilleur score et tendance.
    Les sujets les plus faibles sont affichés en premier."""
    if not historique:
        return "Aucun quiz enregistré pour l'instant. Lance 'quiz <sujet>' pour commencer."

    par_sujet = {}
    for quiz in historique:  # l'historique est dans l'ordre chronologique
        cle = quiz["sujet"].strip().lower()
        scores = par_sujet.get(cle, (None, []))[1]
        scores.append(round(100 * quiz["points"] / quiz["total"]))
        par_sujet[cle] = (quiz["sujet"], scores)  # on garde l'écriture la plus récente

    lignes = ["Ta progression (sujets les plus faibles en premier) :", ""]
    a_revoir = []
    for nom, scores in sorted(par_sujet.values(), key=lambda x: (x[1][-1], x[0].lower())):
        dernier = scores[-1]
        if len(scores) == 1:
            tendance = ""
        elif dernier > scores[-2]:
            tendance = f"  ↑ +{dernier - scores[-2]}"
        elif dernier < scores[-2]:
            tendance = f"  ↓ {dernier - scores[-2]}"
        else:
            tendance = "  ="
        lignes.append(f"- {nom} : {dernier} % au dernier quiz, meilleur {max(scores)} % "
                      f"({len(scores)} quiz){tendance}")
        if dernier < SEUIL_A_REVOIR and nom != SUJET_GENERAL:
            a_revoir.append(nom)

    if a_revoir:
        lignes += ["", f"À réviser en priorité : {', '.join(a_revoir)}. "
                       f"Essaie 'fiche {a_revoir[0]}' puis refais un quiz."]
    return "\n".join(lignes)


# --------------------------------------------------------------------------
# Révision espacée : plus un sujet est maîtrisé, plus on attend avant de le revoir
# --------------------------------------------------------------------------

# (score minimum en %, nombre de jours avant de refaire un quiz), du plus haut au plus bas
INTERVALLES = ((100, 14), (80, 7), (60, 3), (0, 1))


def intervalle(score):
    """Nombre de jours à attendre avant de revoir un sujet, selon le dernier score."""
    return next(jours for minimum, jours in INTERVALLES if score >= minimum)


def sujets_a_revoir(historique, aujourdhui=None):
    """Retourne les sujets dont la date de révision est arrivée, sous forme de
    [(sujet, dernier score, jours de retard)], les plus en retard en premier.
    Les quiz « sujets variés » et ceux sans date valide sont ignorés."""
    aujourdhui = aujourdhui or dt.date.today()
    derniers = {}  # sujet en minuscules -> (sujet, date, score) du quiz le plus récent
    for quiz in historique:
        if quiz["sujet"] == SUJET_GENERAL:
            continue
        try:
            date = dt.datetime.fromisoformat(quiz["date"]).date()
        except (KeyError, TypeError, ValueError):
            continue
        cle = quiz["sujet"].strip().lower()
        if cle not in derniers or date >= derniers[cle][1]:
            derniers[cle] = (quiz["sujet"], date, round(100 * quiz["points"] / quiz["total"]))

    resultat = []
    for sujet, date, score in derniers.values():
        retard = (aujourdhui - date).days - intervalle(score)
        if retard >= 0:
            resultat.append((sujet, score, retard))
    return sorted(resultat, key=lambda x: (-x[2], x[1], x[0].lower()))


def resume_revision(a_revoir):
    if not a_revoir:
        return ("Rien à revoir aujourd'hui. Les sujets reviennent ici quand il est temps "
                "de refaire un quiz : 1 jour après un score sous 60 %, puis 3, 7 ou 14 jours selon ton score.")
    lignes = ["À revoir aujourd'hui :", ""]
    for sujet, score, retard in a_revoir:
        quand = "prévu aujourd'hui" if retard == 0 else f"en retard de {retard} jour{'s' if retard > 1 else ''}"
        lignes.append(f"- {sujet} : {score} % au dernier quiz, {quand}")
    lignes += ["", f"Commence par : quiz {a_revoir[0][0]}"]
    return "\n".join(lignes)


# --------------------------------------------------------------------------
# Fiches de révision : NoteBot résume un sujet de tes notes dans un fichier Markdown
# --------------------------------------------------------------------------

AUCUNE_NOTE = "AUCUNE_NOTE"


def demande_fiche(sujet):
    return (
        f"Rédige une fiche de révision sur « {sujet} » à partir de mes notes. "
        "Consulte d'abord mes notes avec les outils (cherche le sujet et ses mots-clés, puis lis les fichiers pertinents). "
        f"Si mes notes ne parlent pas du tout de ce sujet, réponds uniquement : {AUCUNE_NOTE}\n\n"
        "Sinon, réponds UNIQUEMENT avec la fiche en Markdown, sans phrase d'introduction, avec exactement ce plan :\n"
        f"# Fiche : {sujet}\n"
        "## L'essentiel\n(3 à 5 puces : ce qu'il faut absolument retenir)\n"
        "## Définitions\n(les termes importants, une ligne chacun)\n"
        "## Règles et formules\n(écris « Aucune dans tes notes. » s'il n'y en a pas)\n"
        "## Exemple\n(un exemple concret, avec un bloc de code si c'est du code)\n"
        "## Pièges à éviter\n(les erreurs fréquentes)\n"
        "## Teste-toi\n(3 questions numérotées, sans les réponses)\n"
        "## Sources\n(les fichiers utilisés, avec la ligne ou la page)\n\n"
        "Reste fidèle à mes notes. Si tu ajoutes une information qui n'y est pas, marque-la avec « (hors notes) ». "
        "Sois concis : la fiche doit tenir sur une page."
    )


def nom_fiche(sujet):
    """Transforme un sujet en nom de fichier sûr, ex. « Les pointeurs C++ » -> 'les-pointeurs-c'."""
    sans_accents = unicodedata.normalize("NFKD", sujet).encode("ascii", "ignore").decode()
    nom = re.sub(r"[^a-z0-9]+", "-", sans_accents.lower()).strip("-")[:60].strip("-")
    return nom or "fiche"


def nettoyer_fiche(texte):
    """Retire un éventuel texte avant le titre et les balises ```markdown autour de la fiche."""
    texte = texte.strip()
    texte = re.sub(r"^```(?:markdown|md)?\s*\n", "", texte)
    texte = re.sub(r"\n```\s*$", "", texte)
    # Cherche un vrai titre en début de ligne (« # Fiche… »), pas un « # » au milieu
    # d'une phrase comme « Voici ta fiche sur C# : ».
    titre = re.search(r"^# ", texte, re.MULTILINE)
    return (texte[titre.start():] if titre else texte).strip() + "\n"


def chemin_libre(nom, dossier=None):
    """Retourne <dossier>/<nom>.md, ou <nom>-2.md, <nom>-3.md... pour ne jamais écraser un fichier.
    Le dossier par défaut est celui des fiches."""
    dossier = dossier or FICHES
    chemin = dossier / f"{nom}.md"
    numero = 2
    while chemin.exists():
        chemin = dossier / f"{nom}-{numero}.md"
        numero += 1
    return chemin


def creer_fiche(sujet, client, afficher=print):
    """Rédige une fiche de révision sur `sujet` et l'enregistre dans le dossier des fiches.

    La fiche est générée dans un échange séparé pour ne pas alourdir la conversation.
    Retourne le chemin du fichier créé, ou None si les notes ne couvrent pas le sujet.
    """
    afficher(f"\nJe consulte tes notes pour rédiger la fiche « {sujet} »...")
    texte, outils = repondre([{"role": "user", "content": demande_fiche(sujet)}], client, MAX_TOKENS_FICHE)
    for outil in outils:
        afficher(f"  [outil] {outil['nom']}({outil['arguments']})")
    if AUCUNE_NOTE in texte or not texte.strip():
        afficher(f"\nJe n'ai rien trouvé sur « {sujet} » dans tes notes, donc je n'ai pas créé de fiche. "
                 "Essaie un autre mot-clé, ou ajoute des notes sur ce sujet.")
        return None
    FICHES.mkdir(parents=True, exist_ok=True)
    chemin = chemin_libre(nom_fiche(sujet))
    chemin.write_text(nettoyer_fiche(texte), encoding="utf-8")
    afficher(f"\nFiche enregistrée : {chemin}")
    return chemin


def main():
    DOSSIER.mkdir(parents=True, exist_ok=True)
    if PdfReader is None and any(f.suffix.lower() == ".pdf" for f in fichiers_notes()):
        print("Attention : tu as des notes en PDF, mais le module pypdf n'est pas installé. "
              "Lance 'pip install pypdf' pour que NoteBot puisse les lire.")
    conversation = Conversation(anthropic.Anthropic())
    print("NoteBot prêt ! Pose ta question ('quiz [sujet]' pour t'entraîner, 'fiche <sujet>' pour une fiche de révision, "
          "'progres' pour voir tes résultats, 'revoir' pour les sujets à réviser aujourd'hui, 'notes' pour lister tes notes, 'cherche <mot>' pour une recherche rapide, 'conso' pour les tokens utilisés, 'export' pour enregistrer la conversation, 'reset' pour recommencer, 'quitter' pour sortir).")
    a_revoir = sujets_a_revoir(charger_progres())
    if a_revoir:
        noms = ", ".join(sujet for sujet, _, _ in a_revoir)
        print(f"Rappel : {len(a_revoir)} sujet{'s' if len(a_revoir) > 1 else ''} à revoir aujourd'hui ({noms}). "
              "Tape 'revoir' pour les détails.")

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
        if question.lower() == "export":
            try:
                chemin = exporter_conversation(conversation)
            except OSError as erreur:
                print("\nImpossible d'enregistrer la conversation :", erreur)
                continue
            print(f"\nConversation enregistrée : {chemin}" if chemin else "\nRien à exporter : pose d'abord une question.")
            continue
        if question.lower() == "conso":
            print("\n" + CONSOMMATION.resume(prix_env("NOTEBOT_PRIX_ENTREE"), prix_env("NOTEBOT_PRIX_SORTIE")))
            continue
        if question.lower() == "notes":
            print("\n" + resume_notes())
            continue
        if question.lower() == "cherche" or question.lower().startswith("cherche "):
            print("\n" + recherche_rapide(question[7:]))
            continue
        if question.lower() == "revoir":
            print("\n" + resume_revision(sujets_a_revoir(charger_progres())))
            continue
        if question.lower() in ("progres", "progrès"):
            print("\n" + resume_progres(charger_progres()))
            continue
        if question.lower() == "fiche" or question.lower().startswith("fiche "):
            sujet = question[5:].strip()
            if not sujet:
                print("Précise un sujet, par exemple : fiche pointeurs")
                continue
            try:
                creer_fiche(sujet, conversation.client)
            except anthropic.APIError as erreur:
                print("\nErreur de l'API :", erreur)
            except TropDEtapes as erreur:
                print("\nErreur :", erreur)
            except OSError as erreur:
                print("\nImpossible d'enregistrer la fiche :", erreur)
            continue
        if question.lower() == "quiz" or question.lower().startswith("quiz "):
            sujet = question[4:].strip()
            try:
                verdicts = lancer_quiz(conversation, sujet)
            except anthropic.APIError as erreur:
                print("\nErreur de l'API :", erreur)
                continue
            except TropDEtapes as erreur:
                print("\nErreur :", erreur)
                continue
            try:
                if enregistrer_quiz(sujet, verdicts):
                    print("Résultat enregistré. Tape 'progres' pour voir ton évolution.")
            except OSError as erreur:
                print("\nImpossible d'enregistrer le résultat du quiz :", erreur)
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

    if CONSOMMATION.appels:
        print("\n" + CONSOMMATION.resume(prix_env("NOTEBOT_PRIX_ENTREE"), prix_env("NOTEBOT_PRIX_SORTIE")))
    print("À plus !")


if __name__ == "__main__":
    main()