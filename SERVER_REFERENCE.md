# ComicAI Forge generation server

This is an implemented FastAPI server with real OpenAI Responses and Images API integration. It has no production fixture/demo fallback. Without `OPENAI_API_KEY`, creating a generation job returns HTTP 503.

## Deploy with HTTPS

Requirements: a Linux host with Docker and Compose, a DNS hostname pointing at that host, and access to the chosen OpenAI models. Generation uses the configured API account and can incur provider charges.

1. Copy `.env.example` to `.env` in this directory.
2. In your host's secret settings or local `.env`, set `OPENAI_API_KEY` and `COMIC_DOMAIN`. Never put the provider key in the Android app or source control. The Compose configuration derives the public HTTPS URL from `COMIC_DOMAIN`.
3. Allow inbound TCP ports 80 and 443, then run:

```bash
docker compose up --build -d
docker compose exec server python -m comic_server.manage create-user damico
```

The second command prompts for a password of at least 12 characters, without echoing it. Running it again changes that user's password and revokes their existing sessions. Add other named users through the same command; job access is restricted to its owner.

4. Open `https://YOUR_HOST/health`; check `status: ok` and `generationConfigured: true`.
5. In the Android app, turn demo mode off, enter `https://YOUR_HOST` as the server base URL, and sign in with the created username/password.
6. Generate one page first and verify the finished images and dialogue before submitting a larger comic.

Caddy manages HTTPS certificates. SQLite, server images and the signing key are stored in the `comic_data` volume; back up that volume. Do not delete it during upgrades. Do not run multiple server processes against the same data directory. A process lock enforces that restriction. The queue worker is serial and supports up to three active jobs per user.

## Local development

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
export COMIC_DATA_DIR="$PWD/data"
export COMIC_PUBLIC_URL="https://YOUR_TEST_HTTPS_HOST"
# Set OPENAI_API_KEY through your shell/secret manager; never commit it.
.venv/bin/python -m comic_server.manage create-user damico
.venv/bin/uvicorn comic_server.app:create_app --factory --host 127.0.0.1 --port 8000 --workers 1 --no-access-log
```

The app requires HTTPS. Use your trusted HTTPS reverse proxy for development; the distributed app does not disable certificate checks or permit cleartext traffic. The server does not automatically load `.env` outside Docker Compose; supply variables in the environment.

## API flow

- `POST /v1/login` — username/password to a 24-hour opaque session token.
- `POST /v1/logout` — revoke the bearer session.
- `POST /v1/jobs` — validate and enqueue a request; `Idempotency-Key` must equal `requestId`.
- `GET /v1/jobs` — the authenticated user's latest 50 jobs.
- `GET /v1/jobs/{id}` — status, progress, error and completed comic result.
- `POST /v1/jobs/{id}/cancel` — stop after the current provider call.
- `POST /v1/jobs/{id}/resume` — explicitly continue a failed, interrupted or cancelled job, reusing persisted plan/images.
- `GET /v1/images/{id}/{index}.png?expires=...&signature=...` — signed image delivery.

The server plans four panels per page, preserves a character/setting continuity guide, generates portrait art without lettering, and returns the user's unified dialogue DTO separately. The Android app draws exact speaker text and bubble styles in both preview and PDF. Layouts use normalized coordinates; bubble positions are top-left anchors that the renderer clamps inside the panel.

The default text model is `gpt-4.1-mini` and the default image model is `gpt-image-1`; override through the model environment settings when your account supports other models. These are explicit integration choices, not a claim that they are the latest models. Model availability and output quality depend on the provider/account. A continuity guide improves consistency but does not guarantee identical faces across independently generated images.

Jobs, storyboard plans and completed PNGs persist in SQLite/files. On server restart, an in-flight job becomes `interrupted` and needs an explicit Resume. Completed panels are reused. A provider call already in progress can still finish or incur a charge after cancellation. A process failure between receiving a paid provider response and persisting it can require that one image to be requested again; exactly-once billing across provider/network failures is not claimed. No automatic paid retry occurs.

Image URLs expire after seven days. Authenticated status requests issue fresh URLs; the Android app downloads finished art permanently before committing a local comic. Text, dialogue and art are sent to the chosen AI provider only for cloud generation. Access logs are disabled by the supplied startup command to avoid recording signed image URLs.

## Test

```bash
.venv/bin/python -m pytest -q
```

Provider fixtures and HTTP mocks are injected only in tests. Passing these tests proves local behavior and request contracts, not a successful paid live-provider request. Production `create_app()` always selects the real provider.

Official implementation references:

- https://developers.openai.com/api/reference/resources/images/methods/generate
- https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses
- https://fastapi.tiangolo.com/advanced/events/

## Authenticated uploads and visual references

Version 1.1 adds private file uploads of any type. Each file is limited to 50 MB; each account may keep up to 1 GB or 200 uploaded files. A generation can reference up to 20 files, including at most eight AI reference images. Authentication and job ownership apply to file metadata and downloads as well as generation jobs.

- `PUT /v1/uploads/{uuid}?filename=...` accepts the original byte stream with its content type and an `X-Content-SHA256` checksum. The UUID is an idempotency identity; changing the content/name/type for it returns 409. No base64 encoding is needed on this route.
- `GET /v1/uploads/{uuid}` returns private metadata and the file's usable reference kind.
- `GET /v1/uploads/{uuid}/content` returns the original file as a private attachment with no cache and MIME sniffing disabled.
- `DELETE /v1/uploads/{uuid}` deletes an unused upload. Files referenced by a retained generation job cannot be deleted through this endpoint.
- Add `attachments: [{id, useForAi, label, notes, role}]` to the generation request. Roles are CHARACTER, LOCATION or DOCUMENT. Name each selected character photo before submitting. All listed IDs must belong to the authenticated user.

Selected JPEG/PNG/WebP photos are orientation-corrected and converted to JPEG copies at up to 2048 pixels for AI reference input. Original bytes are retained. Storyboard planning receives labeled visual inputs; each panel uses `/images/edits` with the photos and identity/setting instructions. The default `gpt-image-1` model requests `input_fidelity=high`. Empty-reference jobs continue using `/images/generations`. There is no fake face-lock result or automatic promise of exact likeness.

Selected UTF-8 text, text-bearing PDF and DOCX files supply bounded story/context text. Scanned PDFs do not silently become OCR results. Unsupported/corrupt files are archived with a notice and cannot be selected as AI references until the user supplies a readable version. Archive files are never executed or unpacked generally; DOCX extraction reads only a size-bounded `word/document.xml`.

Google Maps location files contain user-entered place/address/pin/coordinate/era notes. These are used as setting context. The service does not scrape Maps or Street View and does not claim to have analyzed an unseen Maps image. Use user-provided location photos for visible details. No additional Maps API key is required for the app's Maps/Street View links.

The existing SQLite store gains an uploads table without altering old user/session/job rows. Pending and existing requests without attachments remain compatible. The Docker data volume now also holds an `uploads` directory and must be included in backups. Only explicit AI-selected references are sent to the provider; all attached originals are retained on this server for job recovery. API keys stay on the server.

Reference links:

- https://developer.android.com/training/data-storage/shared/documents-files
- https://developers.google.com/maps/documentation/urls/get-started
- https://developers.openai.com/api/reference/resources/images/methods/edit
