"""Fhemni – backend Azure Functions (Python, Model V2).

Endpoints HTTP :
  GET  /api/speech-token   -> jeton AI Speech temporaire pour la page professeur
  GET  /api/negotiate      -> URL de connexion Web PubSub pour un téléphone étudiant
  POST /api/caption        -> reçoit une phrase traduite, la diffuse et l'enregistre
  POST /api/status         -> pause / reprise du cours, annoncée aux étudiants et notée dans la transcription
  POST /api/end-session    -> termine le cours : résumés (AI Language) et cahiers de cours
  GET  /api/summary        -> renvoie le résumé d'un cours dans une langue
  GET  /api/notebook       -> télécharge le cahier du cours (résumé + phrases) dans une langue
  GET  /api/transcript     -> télécharge la transcription brute (JSON Lines), réservé au professeur

Toutes les clés sont lues dans les paramètres d'application (variables d'environnement) :
elles ne sont jamais envoyées au navigateur.
"""
import json
import logging
import os
import re
import urllib.request
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

import azure.functions as func
from azure.ai.textanalytics import TextAnalyticsClient
from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.messaging.webpubsubservice import WebPubSubServiceClient
from azure.storage.blob import BlobServiceClient

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

HUB = "fhemni"
CONTAINER = "transcripts"
SESSION_RE = re.compile(r"^[a-z0-9-]{3,40}$")          # ex. "cloud-1120"
LANG_RE = re.compile(r"^[a-z]{2}(-[A-Za-z]{2,4})?$")    # ex. "en", "ar", "zh-Hans"
ID_RE = re.compile(r"^[0-9a-z-]{1,40}$")                # identifiant d'une phrase, créé par la page prof
TZ_RE = re.compile(r"^[A-Za-z_]+(/[A-Za-z0-9_+-]+){0,2}$")  # ex. "Africa/Casablanca"
STATES = {"paused", "live"}
LANG_NAMES = {"fr": "French (Français)", "en": "English", "ar": "Arabic (العربية)", "es": "Spanish (Español)",
              "de": "German (Deutsch)", "zh": "Chinese (中文)"}


# ---------------------------------------------------------------- utilitaires
def reply(data, status=200):
    return func.HttpResponse(json.dumps(data, ensure_ascii=False), status_code=status,
                             mimetype="application/json")


def error(message, status=400):
    return reply({"error": message}, status)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


def append_line(session: str, line: dict) -> None:
    """Ajoute une ligne JSON à la fin de transcripts/<cours>/transcript.jsonl (append blob)."""
    blob = container().get_blob_client(f"{session}/transcript.jsonl")
    try:
        blob.create_append_blob(if_none_match="*")      # créé une seule fois
    except ResourceExistsError:
        pass
    blob.append_block((json.dumps(line, ensure_ascii=False) + "\n").encode("utf-8"))


def read_rows(cont, session: str) -> list:
    raw = cont.get_blob_client(f"{session}/transcript.jsonl").download_blob().readall()
    rows = []
    for row in raw.decode("utf-8").splitlines():
        if row.strip():
            try:
                rows.append(json.loads(row))
            except ValueError:
                logging.warning("Ligne de transcription illisible ignorée")
    return rows


# ---------------------------------------------------------------- 1. jeton Speech
@app.route(route="speech-token", methods=["GET"])
def speech_token(req: func.HttpRequest) -> func.HttpResponse:
    if not is_professor(req):
        return error("Invalid teacher code.", 401)
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
        return error("Invalid lecture code (3 to 40 characters: lowercase letters, digits, hyphens).")
    # Le téléphone rejoint le groupe du cours. Sans rôle "sendToGroup", il ne peut que recevoir.
    token = pubsub().get_client_access_token(groups=[session], minutes_to_expire=180)
    return reply({"url": token["url"]})


# ---------------------------------------------------------------- 3. une phrase traduite
@app.route(route="caption", methods=["POST"])
def caption(req: func.HttpRequest) -> func.HttpResponse:
    if not is_professor(req):
        return error("Invalid teacher code.", 401)
    body = read_json(req)
    if body is None:
        return error("The request body must be a JSON object.")
    session, texts, cid = body.get("session"), body.get("texts"), body.get("id")
    if not valid_session(session):
        return error("Invalid lecture code.")
    if not isinstance(texts, dict) or not texts:
        return error("The 'texts' field must contain the sentence in each language.")
    if cid is not None and not (isinstance(cid, str) and ID_RE.match(cid)):
        return error("Invalid sentence id.")
    # L'ordre est conservé : la première clé est toujours la langue parlée (texte original).
    texts = {k: str(v) for k, v in texts.items() if isinstance(k, str) and LANG_RE.match(k)}

    partial = bool(body.get("partial"))   # phrase en cours (provisoire) ou définitive
    line = {"type": "partial" if partial else "caption", "t": now_iso(), "texts": texts}
    if cid and not partial:
        line["id"] = cid                  # permet au professeur de retirer la phrase avant le résumé

    # a) d'abord diffuser : les étudiants ne doivent jamais attendre l'enregistrement
    pubsub().send_to_group(session, line, content_type="application/json")

    # b) puis ajouter la phrase définitive à la transcription (les provisoires ne sont pas gardées)
    if partial:
        return reply({"ok": True})
    try:
        append_line(session, line)
    except Exception:  # noqa: BLE001 – on journalise sans bloquer la diffusion
        logging.exception("Échec de l'enregistrement de la transcription")
    return reply({"ok": True})


# ---------------------------------------------------------------- 4. pause / reprise
@app.route(route="status", methods=["POST"])
def session_status(req: func.HttpRequest) -> func.HttpResponse:
    if not is_professor(req):
        return error("Invalid teacher code.", 401)
    body = read_json(req) or {}
    session, state = body.get("session"), body.get("state")
    if not valid_session(session):
        return error("Invalid lecture code.")
    if state not in STATES:
        return error("Invalid state: 'paused' or 'live'.")
    line = {"type": "status", "state": state, "t": now_iso()}
    # Les téléphones affichent « cours en pause » ; la coupure est notée dans la transcription.
    pubsub().send_to_group(session, line, content_type="application/json")
    try:
        append_line(session, line)
    except Exception:  # noqa: BLE001
        logging.exception("Échec de l'enregistrement de la pause")
    return reply({"ok": True})


# ---------------------------------------------------------------- 5. fin du cours : résumés + cahiers
def summarize(text: str, lang: str) -> str:
    client = TextAnalyticsClient(os.environ["LANGUAGE_ENDPOINT"],
                                 AzureKeyCredential(os.environ["LANGUAGE_KEY"]))
    poller = client.begin_abstract_summary([text[:120000]], language=lang, sentence_count=5)
    for result in poller.result():
        if result.is_error:
            raise RuntimeError(result.error.message)
        return " ".join(s.text for s in result.summaries)
    return ""


def parse_time(value: str, tz):
    try:
        return datetime.fromisoformat(value).astimezone(tz)
    except (TypeError, ValueError):
        return None


def kept_rows(rows: list, exclude: set) -> list:
    """Phrases définitives non retirées par le professeur, et marques de pause."""
    out = []
    for r in rows:
        kind = r.get("type", "caption")
        if kind == "caption" and r.get("id") not in exclude:
            out.append(r)
        elif kind == "status":
            out.append(r)
    return out


def sentence_in(row: dict, lang: str) -> str:
    """La phrase dans la langue demandée, sinon le texte original (première langue)."""
    texts = row.get("texts", {})
    return texts.get(lang) or next(iter(texts.values()), "")


def build_notebook(session: str, lang: str, rows: list, summary_text: str, tz) -> str:
    """Cahier lisible : en-tête, résumé, puis chaque phrase avec son heure et les pauses."""
    base = lang.split("-")[0]
    first = next((parse_time(r.get("t"), tz) for r in rows if r.get("t")), None)
    head = [
        "Fhemni · Lecture notebook",
        f"Lecture: {session}",
        f"Date: {first.strftime('%d %B %Y') if first else '—'}",
        f"Language: {LANG_NAMES.get(base, lang)}",
        "",
        "SUMMARY",
        summary_text.strip() or "(summary unavailable in this language)",
        "",
        "TRANSCRIPT",
    ]
    body, paused_at = [], None
    for r in rows:
        t = parse_time(r.get("t"), tz)
        if r.get("type") == "status":
            if r.get("state") == "paused" and paused_at is None:
                paused_at = t
            elif r.get("state") == "live" and paused_at is not None:
                minutes = int((t - paused_at).total_seconds() // 60) if t and paused_at else 0
                body.append(f"       — break: {minutes} min —" if minutes >= 1 else "       — short break —")
                paused_at = None
            continue
        text = sentence_in(r, lang).strip()
        if text:
            body.append(f"{t.strftime('%H:%M') if t else '--:--'}  {text}")
    if paused_at is not None:
        body.append("       — break —")
    return "\n".join(head + (body or ["(no sentences)"])) + "\n"


@app.route(route="end-session", methods=["POST"])
def end_session(req: func.HttpRequest) -> func.HttpResponse:
    if not is_professor(req):
        return error("Invalid teacher code.", 401)
    body = read_json(req) or {}
    session = body.get("session")
    if not valid_session(session):
        return error("Invalid lecture code.")
    exclude = body.get("exclude") or []
    if not isinstance(exclude, list) or len(exclude) > 5000:
        return error("The 'exclude' field must be a list of sentence ids.")
    exclude = {x for x in exclude if isinstance(x, str) and ID_RE.match(x)}
    tz = timezone.utc
    tz_name = body.get("tz")
    if ZoneInfo and isinstance(tz_name, str) and TZ_RE.match(tz_name):
        try:
            tz = ZoneInfo(tz_name)
        except Exception:  # noqa: BLE001 – fuseau inconnu : on garde l'heure UTC
            pass

    cont = container()
    try:
        rows = kept_rows(read_rows(cont, session), exclude)
    except ResourceNotFoundError:
        return error("No transcript for this lecture.", 404)

    by_lang: dict[str, list[str]] = {}
    for r in rows:
        if r.get("type", "caption") == "caption":
            for lang, text in r.get("texts", {}).items():
                if text:
                    by_lang.setdefault(lang, []).append(text)

    results = {}
    for lang, sentences in by_lang.items():
        base = lang.split("-")[0]
        try:
            summary_text = summarize(" ".join(sentences), base)
        except Exception as exc:  # noqa: BLE001 – une langue non prise en charge ne bloque pas les autres
            logging.warning("Résumé impossible en %s : %s", lang, exc)
            summary_text = ""
            results[base] = f"error: {exc}"
        else:
            results[base] = "ok"
        cont.upload_blob(f"{session}/summary-{base}.txt", summary_text.encode("utf-8"), overwrite=True)
        notebook_text = build_notebook(session, lang, rows, summary_text, tz)
        cont.upload_blob(f"{session}/notebook-{base}.txt", notebook_text.encode("utf-8"), overwrite=True)

    pubsub().send_to_group(session, {"type": "summary-ready"}, content_type="application/json")
    return reply({"ok": True, "languages": results, "excluded": len(exclude)})


@app.route(route="summary", methods=["GET"])
def summary(req: func.HttpRequest) -> func.HttpResponse:
    session, lang = req.params.get("session", ""), req.params.get("lang", "")
    if not valid_session(session) or not LANG_RE.match(lang):
        return error("Invalid 'session' or 'lang' parameter.")
    try:
        data = container().get_blob_client(f"{session}/summary-{lang.split('-')[0]}.txt").download_blob().readall()
    except ResourceNotFoundError:
        return error("Summary not available yet.", 404)
    return reply({"summary": data.decode("utf-8")})


# ---------------------------------------------------------------- 6. téléchargements
@app.route(route="notebook", methods=["GET"])
def notebook(req: func.HttpRequest) -> func.HttpResponse:
    """Cahier du cours en texte brut, téléchargé directement par le lien de la page étudiant."""
    session, lang = req.params.get("session", ""), req.params.get("lang", "")
    if not valid_session(session) or not LANG_RE.match(lang):
        return error("Invalid 'session' or 'lang' parameter.")
    base = lang.split("-")[0]
    try:
        data = container().get_blob_client(f"{session}/notebook-{base}.txt").download_blob().readall()
    except ResourceNotFoundError:
        return error("Notebook not available yet: the teacher has not ended the lecture.", 404)
    return func.HttpResponse(data, status_code=200, mimetype="text/plain", charset="utf-8",
                             headers={"Content-Disposition": f'attachment; filename="fhemni-{session}-{base}.txt"'})


@app.route(route="transcript", methods=["GET"])
def transcript(req: func.HttpRequest) -> func.HttpResponse:
    """Transcription brute (JSON Lines, toutes les langues) : réservée au professeur."""
    if not is_professor(req):
        return error("Invalid teacher code.", 401)
    session = req.params.get("session", "")
    if not valid_session(session):
        return error("Invalid lecture code.")
    try:
        data = container().get_blob_client(f"{session}/transcript.jsonl").download_blob().readall()
    except ResourceNotFoundError:
        return error("No transcript for this lecture.", 404)
    return func.HttpResponse(data, status_code=200, mimetype="application/x-ndjson", charset="utf-8")
