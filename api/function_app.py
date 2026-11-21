"""Fhemni – backend Azure Functions (Python, Model V2).

Cinq endpoints HTTP :
  GET  /api/speech-token   -> jeton AI Speech temporaire pour la page professeur
  GET  /api/negotiate      -> URL de connexion Web PubSub pour un téléphone étudiant
  POST /api/caption        -> reçoit une phrase traduite, la diffuse et l'enregistre
  POST /api/end-session    -> termine le cours et génère les résumés (AI Language)
  GET  /api/summary        -> renvoie le résumé d'un cours dans une langue

Toutes les clés sont lues dans les paramètres d'application (variables d'environnement) :
elles ne sont jamais envoyées au navigateur.
"""
import json
import logging
import os
import re
import urllib.request
from datetime import datetime, timezone

import azure.functions as func
from azure.ai.textanalytics import TextAnalyticsClient
from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.messaging.webpubsubservice import WebPubSubServiceClient
from azure.storage.blob import BlobServiceClient

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

HUB = "fhemni"
CONTAINER = "transcripts"
SESSION_RE = re.compile(r"^[a-z0-9-]{3,40}$")   # ex. "cloud-1120"
LANG_RE = re.compile(r"^[a-z]{2}(-[A-Za-z]{2,4})?$")  # ex. "en", "ar", "zh-Hans"


# ---------------------------------------------------------------- utilitaires
def reply(data, status=200):
    return func.HttpResponse(json.dumps(data, ensure_ascii=False), status_code=status,
                             mimetype="application/json")


def error(message, status=400):
    return reply({"error": message}, status)


def is_professor(req: func.HttpRequest) -> bool:
    """Les endpoints du professeur exigent le code secret PROF_CODE (en-tête x-prof-code)."""
    expected = os.environ.get("PROF_CODE", "")
    return bool(expected) and req.headers.get("x-prof-code", "") == expected


def valid_session(value) -> bool:
    return isinstance(value, str) and bool(SESSION_RE.match(value))


def pubsub() -> WebPubSubServiceClient:
    return WebPubSubServiceClient.from_connection_string(os.environ["WEBPUBSUB_CONNECTION"], hub=HUB)


def container():
    service = BlobServiceClient.from_connection_string(os.environ["STORAGE_CONNECTION"])
    client = service.get_container_client(CONTAINER)
    try:
        client.create_container()          # privé par défaut
    except ResourceExistsError:
        pass
    return client


def read_json(req: func.HttpRequest):
    try:
        body = req.get_json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


# ---------------------------------------------------------------- 1. jeton Speech
@app.route(route="speech-token", methods=["GET"])
def speech_token(req: func.HttpRequest) -> func.HttpResponse:
    if not is_professor(req):
        return error("Code professeur invalide.", 401)
    region = os.environ["SPEECH_REGION"]
    request = urllib.request.Request(
        f"https://{region}.api.cognitive.microsoft.com/sts/v1.0/issueToken",
        method="POST", data=b"",
        headers={"Ocp-Apim-Subscription-Key": os.environ["SPEECH_KEY"]})
    with urllib.request.urlopen(request, timeout=10) as r:
        token = r.read().decode()
    # Le jeton expire au bout de 10 minutes : la page en redemande un toutes les 9 minutes.
    return reply({"token": token, "region": region})


# ---------------------------------------------------------------- 2. négociation Web PubSub
@app.route(route="negotiate", methods=["GET"])
def negotiate(req: func.HttpRequest) -> func.HttpResponse:
    session = req.params.get("session", "")
    if not valid_session(session):
        return error("Code de cours invalide (3 à 40 caractères : minuscules, chiffres, tirets).")
    # Le téléphone rejoint le groupe du cours. Sans rôle "sendToGroup", il ne peut que recevoir.
    token = pubsub().get_client_access_token(groups=[session], minutes_to_expire=180)
    return reply({"url": token["url"]})


# ---------------------------------------------------------------- 3. une phrase traduite
@app.route(route="caption", methods=["POST"])
def caption(req: func.HttpRequest) -> func.HttpResponse:
    if not is_professor(req):
        return error("Code professeur invalide.", 401)
    body = read_json(req)
    if body is None:
        return error("Le corps de la requête doit être un objet JSON.")
    session, texts = body.get("session"), body.get("texts")
    if not valid_session(session):
        return error("Code de cours invalide.")
    if not isinstance(texts, dict) or not texts:
        return error("Le champ 'texts' doit contenir la phrase dans chaque langue.")
    texts = {k: str(v) for k, v in texts.items() if isinstance(k, str) and LANG_RE.match(k)}

    partial = bool(body.get("partial"))   # phrase en cours (provisoire) ou définitive
    line = {"type": "partial" if partial else "caption",
            "t": datetime.now(timezone.utc).isoformat(timespec="seconds"), "texts": texts}

    # a) d'abord diffuser : les étudiants ne doivent jamais attendre l'enregistrement
    pubsub().send_to_group(session, line, content_type="application/json")

    # b) puis ajouter la phrase définitive à la transcription (les provisoires ne sont pas gardées)
    if partial:
        return reply({"ok": True})
    try:
        blob = container().get_blob_client(f"{session}/transcript.jsonl")
        try:
            blob.create_append_blob(if_none_match="*")
        except ResourceExistsError:
            pass
        blob.append_block((json.dumps(line, ensure_ascii=False) + "\n").encode("utf-8"))
    except Exception:  # noqa: BLE001 – on journalise sans bloquer la diffusion
        logging.exception("Échec de l'enregistrement de la transcription")
    return reply({"ok": True})


# ---------------------------------------------------------------- 4. fin du cours + résumé
def summarize(text: str, lang: str) -> str:
    client = TextAnalyticsClient(os.environ["LANGUAGE_ENDPOINT"],
                                 AzureKeyCredential(os.environ["LANGUAGE_KEY"]))
    poller = client.begin_abstract_summary([text[:120000]], language=lang, sentence_count=5)
    for result in poller.result():
        if result.is_error:
            raise RuntimeError(result.error.message)
        return " ".join(s.text for s in result.summaries)
    return ""


@app.route(route="end-session", methods=["POST"])
def end_session(req: func.HttpRequest) -> func.HttpResponse:
    if not is_professor(req):
        return error("Code professeur invalide.", 401)
    body = read_json(req)
    session = (body or {}).get("session")
    if not valid_session(session):
        return error("Code de cours invalide.")

    cont = container()
    try:
        raw = cont.get_blob_client(f"{session}/transcript.jsonl").download_blob().readall()
    except ResourceNotFoundError:
        return error("Aucune transcription pour ce cours.", 404)

    by_lang: dict[str, list[str]] = {}
    for row in raw.decode("utf-8").splitlines():
        if row.strip():
            for lang, text in json.loads(row).get("texts", {}).items():
                by_lang.setdefault(lang, []).append(text)

    results = {}
    for lang, sentences in by_lang.items():
        base = lang.split("-")[0]
        try:
            summary = summarize(" ".join(sentences), base)
        except Exception as exc:  # noqa: BLE001 – une langue non prise en charge ne bloque pas les autres
            logging.warning("Résumé impossible en %s : %s", lang, exc)
            summary = ""
            results[base] = f"erreur : {exc}"
        else:
            results[base] = "ok"
        cont.upload_blob(f"{session}/summary-{base}.txt", summary.encode("utf-8"), overwrite=True)

    pubsub().send_to_group(session, {"type": "summary-ready"}, content_type="application/json")
    return reply({"ok": True, "languages": results})


@app.route(route="summary", methods=["GET"])
def summary(req: func.HttpRequest) -> func.HttpResponse:
    session, lang = req.params.get("session", ""), req.params.get("lang", "")
    if not valid_session(session) or not LANG_RE.match(lang):
        return error("Paramètres 'session' et 'lang' invalides.")
    try:
        data = container().get_blob_client(f"{session}/summary-{lang.split('-')[0]}.txt").download_blob().readall()
    except ResourceNotFoundError:
        return error("Résumé pas encore disponible.", 404)
    return reply({"summary": data.decode("utf-8")})
