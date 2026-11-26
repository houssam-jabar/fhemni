# Fhemni — live lecture captions, translated for every student

> *Fhemni* means "understand me" in Moroccan Darija.

The teacher speaks French, English or Moroccan Darija into a laptop. Each student scans a QR code and reads the lecture **live on their phone, in their own language** (French, English, Arabic, Spanish, German, Chinese). When the class ends, every student gets an **AI-written summary** and a **downloadable lecture notebook** in their language.

**Features**

- Teacher chooses the spoken language: French, English or Darija (`fr-FR`, `en-US`, `ar-MA`), or **auto-detect** to switch freely between French and English: AI Speech identifies the language of each sentence (continuous language identification).
- **Pause / resume** (button or Space bar) for a phone call or a private chat: nothing is heard or recorded, the AI Speech connection stays open, students see a "paused" banner and the break is marked in the notebook.
- Before the summary, the teacher can **untick any sentence** (off-topic remark, recognition error): it is left out of the summary and the notebook.
- Students download a **lecture notebook** (summary + time-stamped sentences, in their language); the teacher can download the raw **JSON Lines transcript** in all languages.

![Fhemni live demo: French speech recognized and translated in real time](docs/demo.gif)

*Live demo (30 s): the professor dashboard shows each French sentence forming in real time, then finalized with its English, Arabic and Spanish translations, which are pushed to students' phones at the same moment.*

Built entirely on **Microsoft Azure** with serverless and managed AI services. No virtual machines, and close to €0 when idle.

## Architecture

```mermaid
flowchart LR
    P[Teacher page<br/>browser + mic] -- audio --> S[Azure AI Speech<br/>speech translation]
    S -- translated text --> P
    P -- POST /api/caption --> F[Azure Functions<br/>Python, Flex Consumption]
    F -- send_to_group --> W[Azure Web PubSub<br/>one group per lecture]
    W -- WebSocket push --> T[Student phones]
    F -- append --> B[(Blob Storage<br/>private transcripts)]
    F -- end of class --> L[Azure AI Language<br/>abstractive summary]
    L --> B
    H[Blob Storage<br/>static website] -. serves the pages .-> P
    H -. serves the pages .-> T
```

| Azure service | Role |
|---|---|
| Blob Storage (static website) | Hosts the 3 web pages over HTTPS |
| Blob Storage (private container) | Transcript of each lecture (append blob, JSON Lines), summaries and notebooks |
| Azure Functions (Python v2) | 8 HTTP endpoints, the only component that holds secrets |
| Azure AI Speech | Real-time speech recognition (French, English, Darija) + translation |
| Azure Web PubSub | Pushes each sentence to all phones over WebSocket (pub/sub) |
| Azure AI Language | Abstractive summary of the transcript, per language |

## How it works

1. The student page calls `GET /api/negotiate` and receives a signed Web PubSub URL limited to its lecture group (receive-only).
2. The teacher page gets a 10-minute AI Speech token from `GET /api/speech-token` (refreshed every 9 min) and streams the microphone to AI Speech.
3. In-progress sentences (`partial`) are pushed about once per second so students see text immediately. Final sentences are pushed and then appended to the transcript.
4. `POST /api/status` pauses or resumes the lecture: the phones show a banner and the break is written to the transcript. On the teacher side the recognizer is only stopped, not closed, so resuming is instant.
5. `POST /api/end-session` receives the ids of the sentences the teacher unticked, reads the transcript without them, asks AI Language for a 5-sentence summary in each language, writes one notebook per language, and notifies the phones.
6. `GET /api/notebook` downloads the notebook as a text file; `GET /api/transcript` (teacher code required) downloads the raw JSON Lines transcript.

## Security choices

- **No keys in the browser.** Pages only ever receive short-lived tokens. All keys live in Function App settings.
- **Only the teacher can broadcast.** Write endpoints and the raw transcript require a teacher code checked server-side (`401` otherwise).
- **Input validation.** Lecture codes, language codes, sentence ids and time zones are validated with strict regexes.
- **Least privilege.** Students can't publish, the transcript container is private, CORS only allows the site, and access to the resource group goes through Azure RBAC.
- **Resilience.** The pages reconnect automatically when the AI Speech or WebSocket connection drops.

## Project layout

```
api/            Azure Functions backend (function_app.py, requirements.txt, host.json)
web/            Static site: index, prof (teacher dashboard), etudiant (student view), style
deploy.sh       Idempotent Azure CLI script that creates and deploys everything
```

## Deploy

Requirements: an Azure subscription and Azure Cloud Shell (Bash) or the Azure CLI.

```bash
./deploy.sh      # creates rg-fhemni in France Central, all services, then deploys the code
```

The script generates a teacher code and stores it in `.fhemni.env`, which is git-ignored. To remove everything:

```bash
az group delete -n rg-fhemni --yes
```

## Team

Cloud Computing project, 4th year, Université Internationale de Rabat (UIR), supervised by Pr. Ouassim Karrakchou.

- Houssam Jabar
- Anas Jarniji
- Ilyas Ibrahim
