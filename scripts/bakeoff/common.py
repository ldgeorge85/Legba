# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared plumbing for the bake-off harness: env, reads, records, the paid gate.

READ-ONLY BY CONSTRUCTION. Every Postgres connection this harness opens sets
``default_transaction_read_only = on`` at the session level, so a harness bug
cannot write the live database; the registry is read with GETs plus the one
read-only POST the design names (``/v3/export``). Credentials come from the
checkout's ``.env`` and the vault table, and are never printed or recorded.

THE RECORD (``RECORD_SCHEMA``) — one JSON file per (question, arm) under
``<out>/records/<QID>__<ARM>.json``. See :func:`new_record` for every key; the
fields a grader may see are produced by ``normalise.py`` from this record, the
rest (usage, cost, elapsed, model) never reach a grader (design §4.2 axis 8).

THE PAID GATE (:class:`PaidGate`). A paid arm (B, C, D, E) builds everything up
to the provider call and then REFUSES — status ``dry_run`` — unless the run was
started with ``--execute``, an operator ``--run-ceiling-usd``, and a
``--prices`` file whose prices the operator marked verified against the
invoice (design §7: "Verify both prices against the account's invoice before
G1"). The run ledger (``<out>/run.json``) carries the money spent so far; a
question whose hard stop would cross the run ceiling is refused as
``run_ceiling`` and named, never started (design §7: "the run stops and is
reported as partial with the questions not reached named").
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Iterable

RECORD_SCHEMA = "legba.bakeoff.record/1"
RUN_SCHEMA = "legba.bakeoff.run/1"
HARNESS_DIR = Path(__file__).resolve().parent
REPO_ROOT = HARNESS_DIR.parent.parent

#: Arms that spend money. A, A-r cost nothing marginal (design §7).
PAID_ARMS = frozenset({"B", "C", "D", "E"})

#: The frontier component B, C, D and E read their model from (design §2; the
#: id is historical) and the model design v1.1 pins it to (2026-11-05).
FRONTIER_COMPONENT = "llm.anthropic.opus_4_7"
FRONTIER_MODEL = "claude-opus-5-5"


def frontier_refusal(frontier: dict[str, Any]) -> str | None:
    """None when the frontier component serves the pinned model, else why a
    PAID call must not run: design §2 runs one model across B, C, D and E, and
    v1.1 pins it — a component re-pointed mid-run would void the comparison."""
    served = frontier.get("model_name")
    if served != FRONTIER_MODEL:
        return (
            f"frontier_drift: {FRONTIER_COMPONENT} serves {served!r}, design v1.1 "
            f"pins {FRONTIER_MODEL!r} — re-point the component before a paid run"
        )
    return None

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
RECEIPT_BODY_CHARS = 3000  # design §5.2 N2: "the first 3,000 characters of body text"


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


def load_env(path: Path | None = None) -> None:
    """Load ``KEY=VALUE`` lines from the checkout's ``.env`` into os.environ
    (existing variables win). Values are never printed."""
    env_path = path or Path(os.environ.get("LEGBA_ENV_FILE", "/usr/local/deployments/active/legba/.env"))
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        os.environ.setdefault(key.strip(), val)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def git_sha() -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10, check=True,
        )
        return out.stdout.strip() or None
    except Exception:  # noqa: BLE001 — provenance is best-effort, never fatal
        return None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_as_of(raw: str) -> datetime:
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"--as-of must carry a timezone (got {raw!r})")
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# The registry (GETs + the read-only export POST)
# ---------------------------------------------------------------------------


@dataclass
class Registry:
    base: str
    token: str
    timeout: float = 120.0

    @classmethod
    def from_env(cls) -> "Registry":
        token = os.environ.get("LEGBA_REGISTRY_API_TOKEN", "")
        if not token:
            raise RuntimeError("LEGBA_REGISTRY_API_TOKEN is not set (.env)")
        return cls(os.environ.get("LEGBA_BAKEOFF_REGISTRY", "http://127.0.0.1:8090"), token)

    def _client(self):  # noqa: ANN202 — httpx is imported lazily
        import httpx

        return httpx.Client(
            base_url=self.base, timeout=self.timeout,
            headers={"Authorization": f"Bearer {self.token}"},
        )

    def get(self, path: str, **params: Any) -> Any:
        with self._client() as c:
            r = c.get(path, params={k: v for k, v in params.items() if v is not None})
            r.raise_for_status()
            return r.json()

    def get_or_none(self, path: str, **params: Any) -> Any | None:
        with self._client() as c:
            r = c.get(path, params={k: v for k, v in params.items() if v is not None})
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()

    def export_json(self, body: dict[str, Any]) -> Any:
        """``POST /api/v1/v3/export`` — composes a document, writes nothing."""
        with self._client() as c:
            r = c.post("/api/v1/v3/export", json=body)
            if 400 <= r.status_code < 500:
                from packet_a import ExportRefused

                raise ExportRefused(f"POST /v3/export HTTP {r.status_code}: {r.text[:300]}")
            r.raise_for_status()
            return r.json()

    def stack_component(self, component_id: str) -> dict[str, Any]:
        from legba.data.schemas.env_refs import resolve_config_env_refs

        row = self.get(f"/api/v1/registry/stack/{component_id}")
        # ${env:NAME} endpoint and model fields resolve from this process's
        # environment (source the instance's .env first); unset fails loudly.
        cfg = resolve_config_env_refs(
            (row.get("body") or {}).get("config") or {}, component_id=component_id,
        )

        def raw(key: str) -> Any:
            v = cfg.get(key)
            return v.get("raw") if isinstance(v, dict) else v

        return {
            "component_id": component_id,
            "version": row.get("version"),
            "state": row.get("state"),
            "model_name": raw("model_name"),
            "api_endpoint": raw("api_endpoint"),
            "api_key_ref": raw("api_key"),
        }

    def descriptor_head(self, family: str, descriptor_id: str) -> dict[str, Any] | None:
        return self.get_or_none(f"/api/v1/registry/descriptors/{family}/{descriptor_id}")


# ---------------------------------------------------------------------------
# The gateway — OpenAI-compatible chat on a stack component (graders, drafter)
# ---------------------------------------------------------------------------

#: House rule: temperature 1.0 everywhere, and never a max_tokens (a think
#: alias must be free to think).
CHAT_TEMPERATURE = 1.0

#: ``async chat(messages) -> {"text", "usage", "response_model", ...}`` — the
#: one call shape the grading scripts take, so tests pass a fake.
Chat = Callable[[list[dict[str, str]]], Awaitable[dict[str, Any]]]


def chat_url(api_endpoint: str) -> str:
    """``…/v1/chat/completions`` whether or not the endpoint already ends ``/v1``."""
    base = str(api_endpoint).rstrip("/")
    return f"{base}/chat/completions" if base.endswith("/v1") else f"{base}/v1/chat/completions"


def chat_body(model: str, messages: list[dict[str, str]]) -> dict[str, Any]:
    """The request body: temperature 1.0, NO max_tokens."""
    return {"model": model, "temperature": CHAT_TEMPERATURE, "messages": messages}


def openai_usage(out: dict[str, Any]) -> dict[str, Any]:
    u = out.get("usage") or {}
    details = u.get("completion_tokens_details") or {}
    return {
        "input_tokens": u.get("prompt_tokens"),
        "output_tokens": u.get("completion_tokens"),
        "reasoning_tokens": details.get("reasoning_tokens") if isinstance(details, dict) else None,
        "total_tokens": u.get("total_tokens"),
    }


@dataclass
class ChatRoute:
    """One stack component's chat route, its key resolved from the vault (never printed)."""

    component_id: str
    model: str
    url: str
    version: str | None
    api_key: str = field(default="", repr=False)
    timeout: float = 900.0

    async def complete(self, messages: list[dict[str, str]]) -> dict[str, Any]:  # pragma: no cover — G1 only
        import httpx

        t0 = time.monotonic()
        async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout, connect=15.0)) as client:
            r = await client.post(self.url, json=chat_body(self.model, messages),
                                  headers={"Authorization": f"Bearer {self.api_key}"})
            r.raise_for_status()
            out = r.json()
        choice = (out.get("choices") or [{}])[0]
        return {
            "text": (choice.get("message") or {}).get("content") or "",
            "finish_reason": choice.get("finish_reason"),
            "usage": openai_usage(out),
            "response_model": out.get("model"),
            "elapsed_s": round(time.monotonic() - t0, 2),
        }


async def open_chat_route(reg: Registry, component_id: str) -> ChatRoute:  # pragma: no cover — G1 only
    """The component's endpoint + model off the registry, its key from a read-only vault select."""
    comp = reg.stack_component(component_id)
    async with readonly_conn() as conn:
        key = await resolve_secret(conn, comp["api_key_ref"])
    return ChatRoute(component_id=component_id, model=str(comp["model_name"]),
                     url=chat_url(comp["api_endpoint"]), version=comp.get("version"), api_key=key)


_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)


def extract_json_object(text: str) -> dict[str, Any] | None:
    """The JSON object a model reply carries: the whole reply, else a fenced
    block, else the LAST top-level object that decodes. None when there is none."""
    s = _THINK.sub("", text or "").strip()
    candidates = [s] + [m.strip() for m in _FENCE.findall(s)]
    for c in candidates:
        try:
            obj = json.loads(c)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    dec = json.JSONDecoder()
    found: dict[str, Any] | None = None
    i = s.find("{")
    while i != -1:
        try:
            obj, end = dec.raw_decode(s, i)
        except ValueError:
            i = s.find("{", i + 1)
            continue
        if isinstance(obj, dict):
            found = obj
        i = s.find("{", end)
    return found


def load_run(run_dir: Path) -> dict[str, Any]:
    path = Path(run_dir) / "run.json"
    if not path.is_file():
        raise RuntimeError(f"{path} does not exist — open the run (run_arm.py) first")
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, obj: Any) -> None:
    """Pretty, sorted, UTF-8 — every grading file the harness writes."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Postgres — READ-ONLY sessions only
# ---------------------------------------------------------------------------


def _pg_dsn() -> str:
    host = os.environ.get("LEGBA_BAKEOFF_PG_HOST", "127.0.0.1")
    port = os.environ.get("LEGBA_BAKEOFF_PG_PORT", "5432")
    user = os.environ.get("LEGBA_DATA_PG_USER", "legba")
    pw = os.environ.get("LEGBA_DATA_PG_PASSWORD", "legba")
    db = os.environ.get("LEGBA_BAKEOFF_PG_DB", "legba")
    return f"postgresql://{user}:{pw}@{host}:{port}/{db}"


@asynccontextmanager
async def readonly_conn(dsn: str | None = None) -> AsyncIterator[Any]:
    """One asyncpg connection whose session refuses every write."""
    import asyncpg

    conn = await asyncpg.connect(
        dsn or _pg_dsn(),
        server_settings={
            "default_transaction_read_only": "on",
            "statement_timeout": "120000",
            "application_name": "legba_bakeoff_harness",
        },
    )
    try:
        ro = await conn.fetchval("SHOW default_transaction_read_only")
        if ro != "on":  # pragma: no cover — the server refused the setting
            raise RuntimeError("could not open a read-only session; refusing to run")
        yield conn
    finally:
        await conn.close()


async def resolve_secret(conn: Any, secret_id: str) -> str:
    """A vault secret, decrypted in-process from a READ-ONLY select.

    ``CredentialVault.resolve`` would open a ``PostgresStore`` (whose connect
    runs ``CREATE EXTENSION IF NOT EXISTS age``); this reads the same row with
    the same decrypt primitive and touches nothing else. Never printed.
    """
    import sys

    sys.path.insert(0, str(REPO_ROOT / "src"))
    from legba.data.registry.credentials import CredentialVault

    row = await conn.fetchrow(
        "SELECT nonce, ciphertext FROM stack_credentials "
        "WHERE secret_id = $1 AND is_current",
        secret_id,
    )
    if row is None:
        raise RuntimeError(f"vault has no current secret {secret_id!r}")
    vault = CredentialVault(store=None)  # type: ignore[arg-type] — decrypt only
    return vault.decrypt(bytes(row["nonce"]), bytes(row["ciphertext"])).decode()


# ---------------------------------------------------------------------------
# Receipts — every receipt is a SOURCE DOCUMENT (design §5.2 N2)
# ---------------------------------------------------------------------------


def _payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}


async def resolve_signals(conn: Any, ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Signal id → the receipt the normaliser shows (masthead, dated, titled, 3k body)."""
    wanted = [i for i in dict.fromkeys(str(x) for x in ids) if _UUID_RE.match(i)]
    if not wanted:
        return {}
    rows = await conn.fetch(
        """
        SELECT s.id::text AS id, s.source_id, s.payload, s.fetched_at,
               s.canonical_url,
               (SELECT sd.name FROM source_descriptors sd
                 WHERE sd.descriptor_id = s.source_id AND sd.is_head LIMIT 1) AS masthead
          FROM signals s
         WHERE s.id = ANY($1::uuid[])
        """,
        wanted,
    )
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        p = _payload(r["payload"])
        published = p.get("published_at")
        body = p.get("archived_text") or p.get("raw_body") or p.get("summary") or ""
        out[r["id"]] = {
            "kind": "signal",
            "id": r["id"],
            "masthead": r["masthead"] or r["source_id"],
            "date": str(published) if published else (
                r["fetched_at"].isoformat() if r["fetched_at"] else None
            ),
            "date_word": "published" if published else "fetched",
            "title": p.get("title"),
            "url": p.get("source_url") or p.get("link") or r["canonical_url"],
            "body": str(body)[:RECEIPT_BODY_CHARS],
        }
    for missing in set(wanted) - set(out):
        out[missing] = {"kind": "signal", "id": missing, "resolved": False}
    return out


async def expand_findings(
    conn: Any, ids: Iterable[str], *, depth: int = 2,
) -> dict[str, list[str]]:
    """Finding id → the SIGNAL ids it cites (its ``citations[].signal_id``;
    a cited finding is followed to ITS signals, up to ``depth`` levels; a
    finding with no typed citation falls back to its ``derived_from``)."""
    out: dict[str, list[str]] = {}
    frontier = {str(i) for i in ids if _UUID_RE.match(str(i))}
    rows_by_id: dict[str, Any] = {}
    for _ in range(depth + 1):
        need = [i for i in frontier if i not in rows_by_id]
        if not need:
            break
        rows = await conn.fetch(
            "SELECT id::text AS id, kind, data, derived_from::text[] AS derived "
            "FROM analyst_outputs WHERE id = ANY($1::uuid[])",
            need,
        )
        frontier = set()
        for r in rows:
            rows_by_id[r["id"]] = r
            cites = (_payload(r["data"]).get("data") or {}).get("citations") or []
            for c in cites if isinstance(cites, list) else []:
                if isinstance(c, dict) and c.get("ref_kind") == "finding" and c.get("ref_id"):
                    frontier.add(str(c["ref_id"]))
    signal_ids = {
        str(x) for r in rows_by_id.values() for x in (r["derived"] or [])
    }

    def signals_of(fid: str, level: int) -> list[str]:
        r = rows_by_id.get(fid)
        if r is None:
            return []
        cites = (_payload(r["data"]).get("data") or {}).get("citations") or []
        got: list[str] = []
        for c in cites if isinstance(cites, list) else []:
            if not isinstance(c, dict):
                continue
            if c.get("signal_id"):
                got.append(str(c["signal_id"]))
            elif c.get("ref_kind") == "finding" and c.get("ref_id") and level < depth:
                got.extend(signals_of(str(c["ref_id"]), level + 1))
        if not got:
            got = [x for x in (r["derived"] or []) if str(x) in signal_ids]
        return list(dict.fromkeys(got))

    for fid in {str(i) for i in ids}:
        out[fid] = signals_of(fid, 0)
    return out


# ---------------------------------------------------------------------------
# Records + the run ledger
# ---------------------------------------------------------------------------


def new_record(
    *, run: dict[str, Any], question: Any, arm: str, as_of: datetime,
) -> dict[str, Any]:
    """The empty record every arm fills. Keys are fixed; absent stays ``None``.

    * ``status`` — ``answered`` | ``partial`` (a stop delivered the partial) |
      ``dry_run`` (built to the call, refused) | ``refused`` (a design rule:
      void, run ceiling, unregistered arm) | ``error``.
    * ``label`` — the arm as graded: ``B`` only when the bake-off descriptor
      ran; a stock consult is ``B*``/``C*`` and excluded (design §2).
    * ``raw_answer`` — the arm's text exactly as produced (A: the packet text).
    * ``stated_confidence`` — the arm's own number per design §2 (A: the
      composition's; B/C: 1 − uncertainty; A-r/D/E: the ``Confidence:`` line);
      ``None`` = stated none (reported as coverage, never imputed).
    * ``receipts`` — ``[{ref, kind: signal|finding|url, documents: [...]}]``,
      every document a source (a finding expanded to its signals).
    * ``usage`` / ``cost`` / ``elapsed_s`` / ``model`` — axis 8 only.
    """
    window = question.window_bounds(as_of)
    prior = question.prior_window(as_of)
    return {
        "schema": RECORD_SCHEMA,
        "run_id": run["run_id"],
        "question_id": question.qid,
        "class": question.klass,
        "desk": question.desk,
        "question": question.text,
        "arm": arm,
        "label": arm,
        "as_of": as_of.isoformat(),
        "window": {"start": window[0].isoformat(), "end": window[1].isoformat()},
        "prior_window": (
            {"start": prior[0].isoformat(), "end": prior[1].isoformat()} if prior else None
        ),
        "status": None,
        "refusal": None,
        "raw_answer": None,
        "stated_confidence": None,
        "confidence_source": None,
        "receipts": [],
        "packet": None,
        "usage": {
            "input_tokens": None, "output_tokens": None,
            "cache_read_tokens": None, "cache_write_tokens": None,
            "tool_rounds": None, "calls": None, "web_search_requests": None,
        },
        "cost": {
            "usd": None, "priced": arm in PAID_ARMS, "price_per_m": None,
            "hard_stop_usd": None, "hard_stop_breached": False,
        },
        "elapsed_s": None,
        "model": None,
        "request": None,
        "provenance": {
            "design_sha256": run["design_sha256"],
            "harness_git_sha": run.get("harness_git_sha"),
            "prompt_sha256": None,
        },
        "created_at": utcnow().isoformat(),
    }


def record_path(out: Path, qid: str, arm: str) -> Path:
    return Path(out) / "records" / f"{qid}__{arm}.json"


def write_record(out: Path, record: dict[str, Any]) -> Path:
    path = record_path(out, record["question_id"], record["arm"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    return path


def existing_spent(out: Path, qid: str, arm: str) -> bool:
    """True when a record for (qid, arm) already holds money spent — never re-run."""
    path = record_path(out, qid, arm)
    if not path.is_file():
        return False
    rec = json.loads(path.read_text(encoding="utf-8"))
    return rec.get("status") in ("answered", "partial") and arm in PAID_ARMS


def open_run(out: Path, *, as_of: datetime, design: Any, ceiling: float | None) -> dict[str, Any]:
    """Create or re-open ``<out>/run.json``. Re-opening checks the as-of and the
    design hash match: one run is one T and one frozen design."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "run.json"
    if path.is_file():
        run = json.loads(path.read_text(encoding="utf-8"))
        if run["as_of"] != as_of.isoformat() or run["design_sha256"] != design.sha256:
            raise RuntimeError(
                f"{path} belongs to a different run (as_of {run['as_of']}, design "
                f"{run['design_sha256'][:12]}); use a new --out"
            )
        if ceiling is not None and run.get("run_ceiling_usd") not in (None, ceiling):
            raise RuntimeError("the run ceiling is fixed at the run's first paid question")
        if ceiling is not None:
            run["run_ceiling_usd"] = ceiling
    else:
        run = {
            "schema": RUN_SCHEMA,
            "run_id": hashlib.sha256(f"{as_of.isoformat()}|{design.sha256}".encode()).hexdigest()[:12],
            "as_of": as_of.isoformat(),
            "design_sha256": design.sha256,
            "design_version": design.version,
            "harness_git_sha": git_sha(),
            "normaliser_sha256": sha256_file(HARNESS_DIR / "normalise.py"),
            "run_ceiling_usd": ceiling,
            "spent_usd": 0.0,
            "questions_refused_at_ceiling": [],
            "created_at": utcnow().isoformat(),
        }
    path.write_text(json.dumps(run, indent=1, sort_keys=True), encoding="utf-8")
    return run


def save_run(out: Path, run: dict[str, Any]) -> None:
    (Path(out) / "run.json").write_text(json.dumps(run, indent=1, sort_keys=True), encoding="utf-8")


# ---------------------------------------------------------------------------
# The paid gate + the meter
# ---------------------------------------------------------------------------


@dataclass
class Prices:
    """Per-million-token prices the operator verified against the invoice."""

    model_name: str
    input: float
    output: float
    cache_read: float
    cache_write: float
    web_search_per_1k: float | None
    verified_by: str

    @classmethod
    def load(cls, path: Path, model_name: str) -> "Prices":
        """Refuses a file that is not marked verified, or has no row for the
        model the frontier component actually serves."""
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
        if doc.get("verified_against_invoice") is not True or not doc.get("verified_by"):
            raise RuntimeError(
                f"{path}: prices are not marked verified_against_invoice=true with "
                "a verified_by — the design requires the invoice check before G1"
            )
        row = (doc.get("models") or {}).get(model_name)
        if not isinstance(row, dict):
            raise RuntimeError(f"{path}: no verified price row for {model_name!r}")
        vals = {k: float(row[k]) for k in ("input", "output", "cache_read", "cache_write")}
        if vals["input"] <= 0 or vals["output"] <= 0:
            raise RuntimeError(f"{path}: {model_name} input/output prices must be > 0")
        fee = doc.get("web_search_per_1k_usd")
        return cls(
            model_name=model_name, **vals,
            web_search_per_1k=float(fee) if fee is not None else None,
            verified_by=str(doc["verified_by"]),
        )

    def per_m(self) -> dict[str, float]:
        return {
            "input": self.input, "output": self.output,
            "cache_read": self.cache_read, "cache_write": self.cache_write,
        }

    def cost(self, *, input_tokens: int, output_tokens: int, cache_read: int = 0,
             cache_write: int = 0, searches: int = 0) -> float:
        usd = (
            input_tokens * self.input + output_tokens * self.output
            + cache_read * self.cache_read + cache_write * self.cache_write
        ) / 1_000_000
        if searches:
            if self.web_search_per_1k is None:
                raise RuntimeError("searches were billed but no web_search_per_1k_usd is set")
            usd += searches * self.web_search_per_1k / 1000
        return round(usd, 6)


@dataclass
class PaidGate:
    execute: bool
    run_ceiling_usd: float | None
    prices_path: Path | None

    def refusal(self) -> str | None:
        """Why a paid call may NOT be made on this invocation, or None."""
        if not self.execute:
            return "dry_run: paid arm and --execute was not given"
        if not self.run_ceiling_usd or self.run_ceiling_usd <= 0:
            return "dry_run: no operator --run-ceiling-usd"
        if self.prices_path is None:
            return "dry_run: no verified --prices file"
        return None

    def admits(self, run: dict[str, Any], hard_stop: float) -> str | None:
        """None when a question with ``hard_stop`` fits under the run ceiling."""
        spent = float(run.get("spent_usd") or 0.0)
        ceiling = float(run.get("run_ceiling_usd") or 0.0)
        if spent + hard_stop > ceiling:
            return (
                f"run_ceiling: ${spent:.2f} spent + this question's ${hard_stop:.2f} "
                f"hard stop > the ${ceiling:.2f} run ceiling — not reached"
            )
        return None
