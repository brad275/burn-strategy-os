# BURN Strategy OS

Private Railway pilot for BURN's evidence-led strategy workflow. It is deliberately file-backed and single-user: no Supabase, Edge, GTM integration, multi-user authentication, or scheduled monitoring is included.

## What the pilot does

- accepts a working brief behind a private token gate;
- collects attributed web sources, then runs research, competitive and audience work before reconciling it;
- produces strategy, opportunity mapping, a final Markdown document and a proposed (never automatic) memory update;
- keeps immutable stage attempts, quality findings, feedback, learning events and source snapshots in the mounted data directory;
- retries a failing stage once, then stops visibly for human review.

Client-facing copy is explicitly checked not to present the work as an AI product. Facts must remain traceable to a named source and excerpt.

## Railway configuration

Create a single private Railway service from this repository and configure:

| Variable | Value |
| --- | --- |
| `ANTHROPIC_API_KEY` | sealed Anthropic API key (source collection / web search) |
| `XAI_API_KEY` | sealed xAI API key (Grok 4.6 for every later stage) |
| `XAI_MODEL` | optional; defaults to `grok-4.6` |
| `APP_ACCESS_TOKEN` | sealed, long random private access token |
| `DATA_ROOT` | `/app/data` |
| `APP_ENV` | `pilot` |

Attach one persistent volume at `/app/data`. The included `railway.toml` starts one Uvicorn worker and checks `GET /health`.

Do not put keys in the repository or browser. The access token is exchanged only for an HttpOnly, Secure, SameSite cookie.

## Local verification

```bash
python3 -m pip install -r requirements.txt
PYTHONPATH=src python3 -m unittest discover -s tests -t . -v
DATA_ROOT=/tmp/burn-strategy-os APP_ACCESS_TOKEN=local-only APP_ENV=development \
  python3 -m uvicorn strategy_os.app:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/login`, use `local-only`, create the Pragmatic Play project and start its cycle after configuring `ANTHROPIC_API_KEY` and `XAI_API_KEY`. The test suite uses a fixture provider only; it does not call Anthropic or xAI.

## Foundation controls

The original Stage 2 foundation remains intact and provides:

- validated, versioned record contracts;
- tenant-safe filesystem paths;
- atomic canonical writes;
- immutable stage attempts and feedback events;
- explicit project and cycle lifecycles;
- quality findings and a visible ledger;
- memory proposals that require a human promotion decision.

Run the complete test suite:

```bash
python3 -m unittest discover -s tests -t . -v
```

The canonical storage root is supplied by the application. Tests use temporary directories only.
