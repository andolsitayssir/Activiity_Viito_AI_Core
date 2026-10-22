# activiity ai engine

AI execution engine: a LangGraph pipeline routes natural-language intents to a
per-domain executor that calls the corresponding self-hosted backend **directly**
(no MCP). Every executor was live-verified against its running container.

## Architecture

```
intent -> router -> dispatch -> executor node -> per-backend executor -> backend API
                                        |-> DeferredExecutor (honest no-op for unimplemented)
```

Each backend is a `DomainExecutor` registered once in
`server/execution/executors/registry.py` and gated by an env toggle; the
`executor` graph node looks up the executor for the router's `current_domain`.
Never fabricates success: unresolved targets / failed calls raise
`ExecutorError`, unimplemented actions return `data.deferred=True` with a reason.

## Backends (all live-verified)

| Domain             | Backend                     | Executor (file)                            |
|--------------------|-----------------------------|--------------------------------------------|
| `messaging`        | Chatwoot :3000              | `executors/chatwoot.py`                    |
| `crm/accounting/sales/hr/contacts` | Odoo :8069 | `executors/odoo.py`                        |
| `email_action`     | Stalwart :8089 (JMAP)       | `executors/stalwart.py`                    |
| `office`           | OnlyOffice :8093            | `executors/onlyoffice.py`                  |
| `tasking`          | Vikunja :3456               | `executors/vikunja.py`                     |
| `knowledge_kb`     | Activiitykb :8787 + qdrant :6333 | `executors/rag.py`                    |

Not wired (honest `DeferredExecutor`): `calendaring`, `file_management`
(Nextcloud — skipped by product decision), `general` (RAG panel exposes no REST
chat surface).

### Implemented vs deferred (per executor)

- **Chatwoot**: conversation create/read/status/priority/assign, message send,
  messages list, contact create — all live. `*_read`/billing/agent mgmt deferred.
- **Odoo**: generic `{model}.{method}` (create/write/unlink/copy/search_read/
  action_*) with per-model field maps, comodel resolution via `ir.model.fields`,
  fuzzy target lookup, invoice-line escalation (default account), crm.lead
  defaults. 7 actions live-verified (partner/lead/sale.order/hr.employee/
  account.move posted). Knowledge areas without an Odoo table are deferred.
- **Stalwart**: send (Email/set + EmailSubmission/set), query/get, mailbox
  get/create/rename. `email.blob.*` deferred.
- **OnlyOffice**: convert + download_as (real PDF bytes), command
  version/info/meta/drop/forcesave. wopi/callback/docbuilder/editor/
  insert_image/mail_merge deferred.
- **Vikunja**: task/project/bucket/view/assignee/label/comment/relation/
  attachment/share-link/notification CRUD — all live. Note Vikunja 2.5 inverts
  verbs: create = `PUT` on a collection, update = `POST` on an item.
  `share.project` deferred.
- **RAG**: `kb.answer.grounded` + `kb.search.vector` via `POST /api/chat/ask`,
  `kb.search.fulltext` via qdrant scroll + accent-normalized substring scan,
  `kb.status.batch`/`kb.status.processing` via `/api/runs`, `kb.space.read`
  (distinct UA ids). `kb.ingest.*`, `kb.note.create`, `kb.generate.*`,
  `kb.export.pdf`, space writes deferred (no panel surface).

## Configuration

Executors read credentials from the environment (never hardcoded; `.env` is
gitignored). Relevant variables: `CHATWOOT_URL/ACCESS_TOKEN/ACCOUNT_ID`,
`ODOO_URL/DB/LOGIN/PASSWORD`, `STALWART_URL/USER/PASSWORD`,
`ONLYOFFICE_URL/SECRET`, `VIKUNJA_URL/USERNAME/PASSWORD`,
`RAG_URL/QDRANT_URL/RAG_COLLECTION`.

Registry toggles (`VIITO_ENABLE_*`, default enabled): `CHATWOOT`, `ODOO`,
`STALWART`, `ONLYOFFICE`, `VIKUNJA`, `RAG`.

### First-time setup

1. Copy the example configuration and edit the copy with your local
   credentials. On Windows PowerShell:

   ```
   Copy-Item .env.example .env
   ```

   On macOS/Linux:

   ```
   cp .env.example .env
   ```

2. Set `OPENROUTER_API_KEY` in `.env` if you use the default
   `ROUTER_PROVIDER=openrouter`. For local/offline routing, set
   `ROUTER_PROVIDER=local` instead.
3. Fill in credentials only for the backends you have running. Optional Gmail
   and Telegram integrations can be left blank and disabled.
4. Start the application:

   ```
   docker compose up -d --build
   ```

   The webapp is then available at <http://localhost:8000>. The frontend uses
   the same `.env` through the FastAPI server; `VITE_API_BASE` is only needed
   when running the Vite frontend separately.

The repository does not provide the backend services or their credentials.
Install/start those services separately, or change their `*_URL` values in
`.env`. If an existing `tools-secrets.txt` is available, the optional
`server/execution/generate_env.py` helper can import its supported values into
`.env`; do not commit that file or the generated `.env`.

LLM models (env-overridable, defaults in `server/config.py::model_defaults`):
`CLASSIFIER_MODEL` (domain/action/attributes, `google/gemma-4-31b-it`),
`VALIDATOR_MODEL` (LangGraph semantic validation, `openai/gpt-4o-mini`),
`RAG_ROUTER_MODEL` + `SYNTHESIZER_MODEL` (sent to activiitykdb
`/api/chat/ask` as `model`/`synth_model`, `google/gemma-4-31b-it` and
`google/gemma-3-12b-it`).

## Docker

One container runs the whole Viito application: the FastAPI service **and** the
built React UI (FastAPI serves `server/webapp/frontend/dist` with SPA fallback).

```
docker compose up -d --build
```

- App: http://localhost:8000 (UI) — API under `/api`, health at `/health`.
- The container's `.env` comes from the repo `.env`; backend URLs are overridden
  to `http://host.docker.internal:<port>` in `docker-compose.yml` so the AI
  engine reaches the backends that run on the Docker host (Odoo, Chatwoot,
  Stalwart, OnlyOffice, Vikunja, Activiitykb). Edit them if a backend lives
  elsewhere or is containerized with a service name.
- Persistent state (files, uploads, sessions, chat store, langgraph sqlite
  checkpoint) is bind-mounted from the repo: `memory/`,
  `server/webapp/uploads/`, `server/memory/uploads/`,
  `server/webapp/viito_store.json`, the OnlyOffice session files.
- `docker compose down` stops; add `-v` only if you really want to drop the
  bind-mounted state.
- `requirements.txt` is the pinned runtime dependency set used by the build.

## Tests

```
env\Scripts\python.exe -m pytest server/execution/tests/ -q   # 48 passed
```

Offline mocked tests cover the registry + every executor's contract; live
verification scripts live under the temp opencode dir (`vikunja_docs.json`
holds Vikunja v2's OpenAPI, `activiitykdb_openapi.json` the RAG panel's).

Action → parameter schemas for every domain live in `tools/attr_defs/*.json`.