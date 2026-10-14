# ComicAI Forge — Render server setup

## Server 1.2: flexible comic pages

Generation accepts `panelsPerPage` (3, 4, 5, or 6; default 4), `layoutStyle` (`GRID` or `CINEMATIC`; default `GRID`), and `continuityNotes` (up to 6,000 characters). Older requests and stored four-panel jobs keep their defaults. Progress totals use the selected count, image numbering remains continuous across pages, and result rectangles use the selected layout. Planning uses page ranges of at most five pages with the full script and shared continuity guide, then validates the entire board before requesting artwork.

`GET /v1/capabilities` reports the supported layouts/counts. `GET /v1/mobile-release` advertises only signature-verified published builds. Version 0.3.0 remains a development build until its signature can match the original Android app; the release endpoint intentionally still advertises 0.2.0. Do not advance that manifest to a differently signed or unsigned APK.

This repository contains the Android app's real generation backend, dependency lock, first-login setup, and tests. No API keys, passwords, uploaded photographs, or user database are included.

## Existing Docker service on Render

Render can automatically select Docker when it finds the Dockerfile. This is supported: the container entrypoint detects Render, uses the Render port and public URL, creates the initial sign-in account, and runs the API as the unprivileged `comic` user. It uses the same locked dependencies as the native Python deployment.

For a manually created Docker web service, configure these in its Dashboard:

- Environment: `OPENAI_API_KEY`, `COMIC_BOOTSTRAP_PASSWORD` (at least 12 characters), `COMIC_BOOTSTRAP_USERNAME=damico`, and `COMIC_DATA_DIR=/var/data/comicai`.
- Disk: attach a 5 GB persistent disk at `/var/data`. Creating the directory in the image does not provide persistence. Attach the disk before using the app; files created without it will not be copied onto a newly attached disk.
- Settings: set the health check path to `/health`. Leave Docker Command empty so the image's startup command runs.
- Keep one instance. A manually created service may enable automatic deployments on commits; check its setting before pushing updates.

The entrypoint prepares only the application data directory and drops root privileges before starting the API. For the Docker Compose management command, use `docker compose exec --user comic server python -m comic_server.manage create-user damico` so database files keep the API user's ownership.

## First actions on your phone

1. Install/connect the Render integration in ChatGPT so deployment can be managed in your Render account. Sign into your existing Render account or create one at https://dashboard.render.com/register .
2. Create a **private** GitHub repository named `ComicAI-Forge-Server`: https://github.com/new?name=ComicAI-Forge-Server . Initialize it with a README so it has a `main` branch. Grant the ChatGPT GitHub connection access to this repository. Your existing game and TV repositories are separate projects.
3. Return with the repository URL. The prepared files can then be committed to that repository and the deployment configured. If uploading manually, extract this ZIP and upload its contents as source files at the repository root; uploading only the ZIP does not deploy the app.

## Prepared Render configuration

| Setting | Value |
| --- | --- |
| Service name | comicai-forge-server |
| Runtime | Native Python, latest patch of Python 3.12 |
| Region | Virginia |
| Compute | 0.5 CPU / 512 MB (`0.5c-512mb`, formerly Starter) |
| Persistent disk | 5 GB, mounted at `/var/data` |
| Data directory | `/var/data/comicai` |
| Build | `pip install -r requirements.lock.txt` |
| Start | `python -m comic_server.render_start` |
| Health path | `/health` |
| Processes / instances | One worker process in one service instance |
| Automatic deployments | Off; deploy updates deliberately |

The base estimate is $7/month compute + $1.25/month for 5 GB of disk, about **$8.25/month before AI usage, taxes, workspace-plan fees, and usage overages**. Review Render's current checkout estimate before creating the paid service. This is the smallest paid compute plan; large documents or heavy concurrent use may need a larger instance. A free web instance cannot attach the disk this SQLite/files backend needs.

Sources checked October 14, 2026:
- https://render.com/pricing
- https://render.com/articles/how-much-does-cloud-application-hosting-cost-for-small-businesses
- https://render.com/docs/disks
- https://render.com/docs/compute-plans

## Secret values entered privately in Render

When importing `render.yaml` using **New → Blueprint**, Render prompts for:

- `OPENAI_API_KEY`: a project API key from your OpenAI API account. Configure billing and access to the selected models. API usage is separate from a ChatGPT subscription (https://help.openai.com/en/articles/9039756-managing-billing-for-chatgpt-and-the-api-platform). Create/manage keys at https://platform.openai.com/api-keys .
- `COMIC_BOOTSTRAP_PASSWORD`: choose a new app sign-in password with at least 12 characters. This is the password for the ComicAI Forge Android app, not your Render password.

The initial app username is **damico**. Neither secret belongs in this ZIP, GitHub, an app settings field, or a chat message. Enter secret values directly in Render.

The startup script automatically copies Render's actual `RENDER_EXTERNAL_URL` into `COMIC_PUBLIC_URL` for signed image links. You do not need to invent a domain name or buy one. For an existing custom domain, set `COMIC_PUBLIC_URL` explicitly to that HTTPS base URL.

The first startup creates the initial user in the persistent database. Restarts and subsequent deployments keep the existing password and sessions; changing the bootstrap secret does not reset an existing account. After successful first sign-in, you may remove `COMIC_BOOTSTRAP_PASSWORD` from Render's environment. For an intentional password reset, use the existing `python -m comic_server.manage create-user damico` management command in the running service's Shell; it prompts privately and revokes that user's old sessions.

Do not put account creation in Render's build command, pre-deploy command, or a one-off job: those do not have access to this service's persistent disk.

## Verify after deployment

1. Wait for the actual Render deploy to report Live.
2. Open the service's assigned HTTPS URL followed by `/health`. Expect `status: ok`. `generationConfigured: true` means a provider key is present; it does not by itself prove the key is valid, billing is active, or the selected models are accessible.
3. On the phone, open ComicAI Forge, turn **Demo storyboard mode** off, enter the assigned HTTPS **base** URL (without `/health`), and sign in as **damico** with your bootstrap password.
4. Generate **one page** first. The server makes four panel images per page, so even this test is a paid AI request. Review faces, references, dialogue, and locations, then save a PDF before submitting a longer book.
5. Recheck server jobs after an interrupted deploy. Use Resume explicitly to continue from persisted panels; do not start a duplicate job automatically.

The supplied backend defaults remain `gpt-4.1-mini` for planning and `gpt-image-1` for artwork. They are explicit integration choices, not a claim that they are the newest models. Model access and generation must be confirmed on the user's actual API account. No live paid generation is claimed by local tests.

## Preserve your data

Keep `/var/data` attached to this service across updates. SQLite, signing keys, finished panel images, and original uploaded files live under `/var/data/comicai`. Do not delete or replace the disk during an upgrade. A disk-backed service uses a single instance. Keep database-aware backups together with images, uploads, and signing keys; Render advises against restoring a raw disk snapshot as a custom-database recovery method. Details: https://render.com/docs/disks .

## Local checks

```sh
python -m venv .venv
.venv/bin/pip install -r requirements.lock.txt
.venv/bin/python -m pytest -q
```

Tests use local fixtures and mocked provider calls. For the existing API contracts, privacy limits, Docker/VPS alternative, and upload/reference behavior, see `SERVER_REFERENCE.md`.

Official deployment references:
- https://render.com/docs/blueprint-spec
- https://render.com/docs/python-version
- https://render.com/docs/environment-variables
- https://render.com/docs/web-services
