"""mem0_scope_lib — shared scope taxonomy, classifier and Qdrant helpers.

Used by `mem0-scope-audit` (sample-based corpus composition report) and by
`mem0-scope-backfill` (writes the `scope` payload key over the whole store).

Design rules that must not drift:

  * DEFAULT-CLOSED. Anything the classifier cannot place lands in
    the configured personal scope. Widening a row's audience is deliberate.
  * NON-DESTRUCTIVE. Nothing here writes memory text or deletes a point. The
    only mutation offered is Qdrant `set_payload` on the single `scope` key.
  * The LLM only labels. Python does the label -> scope mapping, so the rule
    set is inspectable and testable without a model in the loop.

Classification uses the configured local Ollama endpoint plus any explicit
``BORG_CLASSIFIER_ENDPOINTS`` comma-separated URLs.
"""

from __future__ import annotations

import json
import os
import re
import threading
import importlib.machinery
from pathlib import Path

try:
    from borg_config import CONFIG
except ModuleNotFoundError:
    CONFIG = importlib.machinery.SourceFileLoader(
        "borg_config_scope_lib", str(Path(__file__).resolve().parent / "borg_config.py")
    ).load_module().CONFIG

BASE = CONFIG.mem0_root
DATA = CONFIG.data_root
COLLECTION = str(CONFIG.values["BORG_QDRANT_COLLECTION"])
QDRANT_URL = str(CONFIG.values["BORG_QDRANT_URL"])
_registry_value = os.environ.get("BORG_CLIENT_REGISTRY", "").strip()
REGISTRY = Path(_registry_value).expanduser().resolve() if _registry_value else None

MODEL = str(CONFIG.values["BORG_EXTRACTION_MODEL"])
FALLBACK_MODEL = MODEL
ENDPOINTS = [str(CONFIG.values["BORG_OLLAMA_URL"])]
ENDPOINTS.extend(
    item.strip().rstrip("/")
    for item in os.environ.get("BORG_CLASSIFIER_ENDPOINTS", "").split(",")
    if item.strip() and item.strip().rstrip("/") not in ENDPOINTS
)

# ---------------------------------------------------------------- taxonomy --

SCOPE_PERSONAL = str(CONFIG.values["BORG_MEMORY_SCOPE"])
SCOPE_PROJECT = "team:project"
SCOPE_OPS = "ops"
SCOPE_PERSON_PREFIX = "person:"
SCOPE_CLIENT_PREFIX = "client:"

# Audit-facing categories the model emits (kept short so the model repeats them
# reliably in JSON):
#   client  - work for a named client/prospect in the registry
#   project - internal product and engineering work
#   system  - the machine, the fleet, memory/agent tooling, CI, process metadata
#             (label is "system", not "ops", so the model cannot confuse it with
#              the phrase "Operating Agreement" — observed collision 2026-08-20)
#   personal- the owner's private life: family, health, personal money, home
#   other   - another named living person's private (non-work) matter
#   noise   - contentless, garbled, or pure conversational filler
CATEGORIES = ("client", "project", "system", "personal", "other", "noise")

# Credential-adjacent text never leaves the owner's scope.
_SECRET_RE = re.compile(
    r"\b(api[_ -]?key|secret[_ -]?key|client[_ -]?secret|access[_ -]?token|"
    r"bearer[_ -]?token|refresh[_ -]?token|private[_ -]?key|password|passwd|"
    r"passphrase|credential|ssh[_ -]?key|recovery[_ -]?code|session[_ -]?cookie|"
    r"2fa|mfa code|otp code)\b",
    re.I,
)
# Obvious personal-life markers used as a floor under the model.
# Health words are qualified: a bare \bmedical\b matched the company name
# a company name and pushed an ordinary business row into the personal scope
# (observed 2026-08-20). Match the personal usage, not the word.
_PERSONAL_RE = re.compile(
    r"\b(my (wife|husband|son|daughter|kid|kids|child|children|mom|mother|dad|"
    r"father|family)|owner's (wife|husband|son|daughter|kids|family|health)|"
    r"bank account|credit card|social security|ssn\b|mortgage|paycheck|"
    r"his salary|her salary|owner's salary|"
    r"medical (record|records|history|appointment|condition|bill|bills|leave)|"
    # "diagnosis" must carry a health qualifier. Bare \bdiagnosis\b matched
    # "read-only diagnosis of Codex installation" and privatised two ordinary
    # engineering rows (found by hand-verification 2026-08-20).
    r"prescription|(medical|health|doctor's) diagnosis|therapist)\b",
    re.I,
)
# Company ownership / money / employment internals. Employee-visible client work
# is fine; who owns what, who is paid what, and what the company earns is not.
_COMPANY_CONFIDENTIAL_RE = re.compile(
    r"\b(operating agreement|member(ship)? interest|vested interest|equity (stake|split|grant)|"
    r"cap table|shareholder|profit(-| )sharing|distribution of retained|"
    r"gross receipts|revenue (was|of|totall?ed)|monthly recurring revenue|\bMRR\b|"
    # "commission schedule" is the phrasing that actually appears in the store;
    # "commission rate" alone missed it (found 2026-08-20 while testing floors)
    r"payroll|compensation package|commission (rate|rates|schedule|structure|percentage|allocation)s?|"
    r"severance|termination of employment|"
    r"stripe (gross|net|payout)|invoice total|accounts (payable|receivable))\b",
    re.I,
)


def load_clients() -> dict[str, dict]:
    """slug -> {name, aliases, status} from the client registry."""
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - py<3.11
        import tomli as tomllib  # type: ignore
    if REGISTRY is None or not REGISTRY.exists():
        return {}
    doc = tomllib.loads(REGISTRY.read_text())
    out = {}
    for slug, c in (doc.get("clients") or {}).items():
        out[slug] = {
            "name": c.get("name", slug),
            "aliases": c.get("aliases", []),
            "status": c.get("status", ""),
            "repos": c.get("repos", []),
            "contacts": c.get("contacts", []),
        }
    return out


def client_menu(clients: dict[str, dict]) -> str:
    lines = []
    for slug, c in sorted(clients.items()):
        names = [c["name"], *c["aliases"], *c.get("contacts", [])]
        seen, uniq = set(), []
        for n in names:
            if n and n.lower() not in seen:
                seen.add(n.lower())
                uniq.append(n)
        lines.append(f"  {slug} = {', '.join(uniq[:6])}")
    return "\n".join(lines)


# Rows mined from a teammate's own machine. Used ONLY by the opt-in
# --origin-default mode; see origin_person().
def _origin_people() -> dict[re.Pattern, str]:
    raw = os.environ.get("BORG_ORIGIN_PEOPLE_JSON", "").strip()
    if not raw:
        return {}
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("BORG_ORIGIN_PEOPLE_JSON must be a JSON object") from exc
    if not isinstance(values, dict) or not all(
        isinstance(marker, str) and marker and isinstance(person, str) and person
        for marker, person in values.items()
    ):
        raise ValueError("BORG_ORIGIN_PEOPLE_JSON must map source markers to people")
    return {re.compile(re.escape(marker), re.I): person for marker, person in values.items()}


ORIGIN_PERSON = _origin_people()

# Reasons that must never be softened by origin-default: the regex floors and an
# explicit sensitivity flag mean owner-only regardless of source machine
# from.
HARD_PERSONAL_REASONS = (
    "credential-adjacent text (regex floor)",
    "personal-life text (regex floor)",
    "company-confidential text (regex floor)",
    "model flagged sensitive",
)


def origin_person(source: str) -> str | None:
    """Which teammate's machine a row was mined from, if any."""
    for pat, name in ORIGIN_PERSON.items():
        if pat.search(source or ""):
            return name
    return None


# ------------------------------------------------- identifier floors (ops) --
#
# The audit found live infrastructure identifiers scattered through ordinary
# work rows: a staging database project reference in 232 rows, and an
# automation account named with its ADMIN permission in 78. Topic-based
# classification cannot contain those — a sentence about where staging is
# hosted is genuinely engineering work, and the hostname rides along with it.
#
# Director ruling 2026-08-20: force any row carrying such an identifier to
# `ops`, which no teammate grant includes. Roughly 105 ordinary sentences get
# over-closed as a result. That is accepted: it is utility loss, not a leak.
#
# The identifier VALUES live in data/scope-identifier-floors.json (mode 600),
# not in this file, which is world-readable. Losing the file loses the floors,
# so `identifier_floors()` reports when it is missing rather than failing quiet.

IDENTIFIER_FLOOR_FILE = DATA / "scope-identifier-floors.json"
_floor_cache: list[dict] | None = None


def load_identifier_floors(path: Path | None = None) -> list[dict]:
    """Compile the floor rules. Each rule is {name, scope, match, and?}.

    `match` must hit. If `and` is present it must hit too — that is how an
    account name is distinguished from the same account name stated together
    with its permission level.
    """
    p = path or IDENTIFIER_FLOOR_FILE
    if not p.exists():
        return []
    rules = []
    for r in json.loads(p.read_text()).get("floors", []):
        try:
            rules.append(
                {
                    "name": r["name"],
                    "scope": r.get("scope", SCOPE_OPS),
                    "match": re.compile(r["match"], re.I),
                    "and": re.compile(r["and"], re.I) if r.get("and") else None,
                }
            )
        except (KeyError, re.error):
            continue
    return rules


def identifier_floors(reload: bool = False) -> list[dict]:
    global _floor_cache
    if _floor_cache is None or reload:
        _floor_cache = load_identifier_floors()
    return _floor_cache


def match_identifier_floor(text: str, rules=None) -> dict | None:
    if not text:
        return None
    for r in rules if rules is not None else identifier_floors():
        if r["match"].search(text) and (r["and"] is None or r["and"].search(text)):
            return r
    return None


def guess_client(text: str, clients: dict[str, dict]) -> str | None:
    """Recover a client slug when the model said "client" but gave no slug.

    Literal, case-insensitive, word-boundary match on the client's own name and
    aliases only — never on contact names, which are common first names and
    would mis-attach. Two different clients matching means ambiguous, so it
    returns None and the caller defaults closed.
    """
    if not text:
        return None
    hits = set()
    for slug, c in clients.items():
        for alias in [c.get("name", ""), *c.get("aliases", [])]:
            if len(alias) < 4:
                continue
            if re.search(r"(?<![A-Za-z0-9])" + re.escape(alias) + r"(?![A-Za-z0-9])", text, re.I):
                hits.add(slug)
                break
    return hits.pop() if len(hits) == 1 else None


def to_scope(cat: str, slug: str, sensitive: bool, clients: dict, text: str = "",
             floors=None) -> tuple[str, str]:
    """Map a model label to a scope. Returns (scope, reason).

    Default-closed: every path that is not confidently shareable resolves to
    the configured personal scope.

    Order matters, most restrictive first. The personal floors and the
    model's own sensitivity flag are checked before the identifier floors, so a
    row carrying both a staging hostname and a commission rate stays owner-only
    rather than being relaxed to `ops`.
    """
    if text and _SECRET_RE.search(text):
        return SCOPE_PERSONAL, "credential-adjacent text (regex floor)"
    if text and _PERSONAL_RE.search(text):
        return SCOPE_PERSONAL, "personal-life text (regex floor)"
    if text and _COMPANY_CONFIDENTIAL_RE.search(text):
        return SCOPE_PERSONAL, "company-confidential text (regex floor)"
    if sensitive:
        return SCOPE_PERSONAL, "model flagged sensitive"
    hit = match_identifier_floor(text, floors)
    if hit:
        return hit["scope"], f"identifier floor: {hit['name']}"
    cat = (cat or "").strip().lower()
    if cat == "client":
        slug = (slug or "").strip().lower()
        if slug in clients:
            return f"{SCOPE_CLIENT_PREFIX}{slug}", "registry slug"
        recovered = guess_client(text, clients)
        if recovered:
            return f"{SCOPE_CLIENT_PREFIX}{recovered}", "slug recovered by literal alias match"
        return SCOPE_PERSONAL, f"unknown client slug {slug!r} -> default-closed"
    if cat == "project":
        return SCOPE_PROJECT, "internal project work"
    if cat in ("system", "ops"):
        return SCOPE_OPS, "machine/fleet/tooling"
    if cat == "noise":
        return SCOPE_OPS, "contentless filler"
    if cat in ("personal", "other"):
        return SCOPE_PERSONAL, "personal / other person's private matter"
    return SCOPE_PERSONAL, f"unrecognised category {cat!r} -> default-closed"


# --------------------------------------------------------------- prompting --

PROMPT_HEAD = """You classify short memory notes from one person's AI-assistant \
memory store. The owner identifier is {owner}. Their project context is: {context}.

For EACH numbered note output one JSON object:
  {{"i": <the note number>, "c": "<category>", "s": "<client slug or empty>", "p": <0 or 1>}}

category "c" must be exactly one of:
  client   - work about a specific named client or prospect below (their site, \
report, campaign, emails with them, their repo, their meetings)
  project  - internal product or engineering work not tied to one client: \
software, agent tooling, repositories, reviews, CI, and reports
  system   - the machines, ports, daemons, memory system, model routing, agent \
process bookkeeping, session metadata, task status noise
  personal - the owner's own private life: family, school, health, personal \
money, home, shopping, travel, personal accounts
  other    - a different named living person's PRIVATE, non-work matter
  noise    - contentless, garbled, or pure conversational filler

"other" is rare. Use it only for someone's private life. A note that merely \
NAMES a person — their job title, their company, an introduction, a vendor \
pitch, a sales email, a meeting request, who reports to whom, what they said \
about a project — is ordinary business: use "client" if it is about one client \
below, otherwise "project". When unsure between "other" and "project", choose \
"project".

"s" = the client slug, ONLY when c is "client". Otherwise "".
If you choose c="client" you MUST give a slug from this list. If none of these \
clients fits, the note is not client work — use "project" instead.
Client slugs:
{menu}

"p" = 1 when the note holds material that must never be shared with an employee.
Set p=1 for ANY of:
  - the owner's private personal or family life, health, home, or personal money
  - anything credential-adjacent: tokens, API keys, passwords, cookies, recovery codes
  - company ownership or equity: operating agreements, member/vested interests, \
cap table, profit splits, partner or shareholder arrangements
  - company money: revenue, gross receipts, payouts, payroll, salaries, \
commissions, someone's compensation
  - employment or legal matters about a named person: hiring terms, termination, \
disputes, complaints
Otherwise p=0. Ordinary client project work, deliverables, websites, reports, \
rankings and engineering detail are p=0 even when commercially confidential.

Output ONLY a JSON array of {n} objects, in order, no prose, no markdown fence.

NOTES:
"""


def build_prompt(batch: list[dict], menu: str, maxlen: int = 300) -> str:
    lines = []
    for k, r in enumerate(batch):
        t = (r.get("data") or "").replace("\n", " ").strip()
        if len(t) > maxlen:
            t = t[:maxlen] + "…"
        lines.append(f"{k}. {t}")
    context = os.environ.get("BORG_SCOPE_CONTEXT", "configured private and project work")
    return PROMPT_HEAD.format(
        menu=menu, n=len(batch), owner=str(CONFIG.values["BORG_OWNER_ID"]),
        context=context,
    ) + "\n".join(lines)


_JSON_RE = re.compile(r"\[.*\]", re.S)


def parse_labels(raw: str, n: int) -> list[dict] | None:
    m = _JSON_RE.search(raw or "")
    if not m:
        return None
    try:
        arr = json.loads(m.group(0))
    except Exception:
        # tolerate trailing commas / truncation by rebuilding object-by-object
        objs = re.findall(r"\{[^{}]*\}", m.group(0))
        arr = []
        for o in objs:
            try:
                arr.append(json.loads(o))
            except Exception:
                pass
        if not arr:
            return None
    by_i = {}
    for o in arr:
        if not isinstance(o, dict):
            continue
        try:
            i = int(o.get("i"))
        except Exception:
            continue
        by_i[i] = {
            "c": str(o.get("c", "")).strip().lower(),
            "s": str(o.get("s", "") or "").strip().lower(),
            "p": 1 if str(o.get("p", 0)).strip() in ("1", "true", "True") else 0,
        }
    return [by_i.get(k, {"c": "", "s": "", "p": 0}) for k in range(n)]


# -------------------------------------------------------------- classifier --


class Classifier:
    """Fans batches out across the live Ollama endpoints, round-robin per worker."""

    def __init__(self, endpoints=None, model=MODEL, log=None, origin_default=False):
        self.model = model
        self.log = log
        # origin_default: when a row cannot be placed and it was mined from a
        # teammate's own machine, file it under person:<them> instead of
        # the owner's personal scope. It is their own working context, so their bucket is
        # the closed default for it. OFF unless explicitly asked for — it
        # widens who can read an unplaceable row, which is a decision for the
        # owner, not a default.
        self.origin_default = origin_default
        self.endpoints = endpoints or self.live_endpoints()
        if not self.endpoints:
            raise RuntimeError("no Ollama endpoint answered")
        self._lock = threading.Lock()
        self.clients = load_clients()
        self.menu = client_menu(self.clients)

    @staticmethod
    def live_endpoints(candidates=None, model=MODEL) -> list[str]:
        import urllib.request

        live = []
        for url in candidates or ENDPOINTS:
            try:
                with urllib.request.urlopen(url + "/api/tags", timeout=4) as r:
                    tags = json.load(r)
                if any(m["name"] == model for m in tags.get("models", [])):
                    live.append(url)
            except Exception:
                pass
        return live

    def _chat(self, host: str, prompt: str, n: int) -> str:
        import ollama

        c = ollama.Client(host=host, timeout=300)
        kw = {"think": False} if self.model.startswith("qwen3") else {}
        r = c.chat(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            options={"temperature": 0, "num_predict": 60 * n + 200},
            **kw,
        )
        return r["message"]["content"]

    def classify_batch(self, batch: list[dict], host: str) -> list[dict]:
        """Returns one record per input row: {id, cat, slug, sensitive, scope, reason}."""
        prompt = build_prompt(batch, self.menu)
        labels = None
        for attempt in range(2):
            try:
                labels = parse_labels(self._chat(host, prompt, len(batch)), len(batch))
            except Exception as e:
                self._say(f"batch error on {host} attempt {attempt}: {e}")
                labels = None
            if labels:
                break
        if not labels:
            labels = [{"c": "", "s": "", "p": 0}] * len(batch)  # default-closed
        out = []
        for r, lab in zip(batch, labels):
            scope, reason = to_scope(lab["c"], lab["s"], bool(lab["p"]), self.clients, r.get("data", ""))
            if (
                self.origin_default
                and scope == SCOPE_PERSONAL
                and reason not in HARD_PERSONAL_REASONS
            ):
                person = origin_person(r.get("source", ""))
                if person:
                    scope = f"{SCOPE_PERSON_PREFIX}{person}"
                    reason = f"unplaceable, mined from {person}'s machine -> their own bucket"
            out.append(
                {
                    "id": r["id"],
                    "cat": lab["c"] or "unclassified",
                    "slug": lab["s"],
                    "sensitive": bool(lab["p"]) or reason.endswith("(regex floor)"),
                    "scope": scope,
                    "reason": reason,
                }
            )
        return out

    def run(self, rows: list[dict], batch_size: int, out_path: Path, done_ids: set[str] | None = None):
        """Classify `rows`, appending one JSON record per row to out_path.

        One worker thread per endpoint, each PULLING the next batch when it is
        free. A slow endpoint therefore takes fewer batches instead of stalling
        the run; an ordered map would block the whole pipeline on that endpoint.

        Resumable: rows already in done_ids are skipped. Each batch is appended
        and fsynced as it lands, so a kill loses at most one batch.
        Yields (n_done, n_total).
        """
        import queue as _q
        import threading as _t

        done_ids = done_ids or set()
        todo = [r for r in rows if r["id"] not in done_ids]
        work_q: "_q.Queue[list]" = _q.Queue()
        for i in range(0, len(todo), batch_size):
            work_q.put(todo[i : i + batch_size])
        results: "_q.Queue[list]" = _q.Queue()
        fh = open(out_path, "a")
        n_done = 0

        def worker(host):
            while True:
                try:
                    batch = work_q.get_nowait()
                except _q.Empty:
                    return
                try:
                    results.put(self.classify_batch(batch, host))
                except Exception as e:
                    self._say(f"{host} failed hard ({e}); re-queueing batch and retiring endpoint")
                    work_q.put(batch)
                    return

        threads = [_t.Thread(target=worker, args=(h,), daemon=True) for h in self.endpoints]
        for t in threads:
            t.start()
        while n_done < len(todo):
            try:
                recs = results.get(timeout=5)
            except _q.Empty:
                if not any(t.is_alive() for t in threads):
                    self._say(f"all endpoints retired with {len(todo)-n_done} rows outstanding")
                    break
                continue
            for rec in recs:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
            n_done += len(recs)
            yield n_done, len(todo)
        fh.close()

    def _say(self, msg: str):
        if self.log:
            self.log(msg)


# ------------------------------------------------------------------ qdrant --


def qdrant():
    from qdrant_client import QdrantClient

    return QdrantClient(url=QDRANT_URL, timeout=120)


def scroll_all(q, with_payload=True, page=2000, limit=None):
    """Yield every point in the collection. Read-only."""
    off, n = None, 0
    while True:
        pts, off = q.scroll(COLLECTION, limit=page, offset=off, with_payload=with_payload, with_vectors=False)
        if not pts:
            return
        for p in pts:
            yield p
            n += 1
            if limit and n >= limit:
                return
        if off is None:
            return


def set_scope(q, id_scope_pairs: list[tuple[str, str]]):
    """Add/overwrite ONLY the `scope` key on the given point ids.

    Uses Qdrant set_payload, which merges into the existing payload. It never
    touches `data`, vectors, or any other key, and never deletes a point.
    """
    from collections import defaultdict

    by_scope = defaultdict(list)
    for pid, scope in id_scope_pairs:
        by_scope[scope].append(pid)
    for scope, ids in by_scope.items():
        q.set_payload(COLLECTION, payload={"scope": scope}, points=ids, wait=True)
    return sum(len(v) for v in by_scope.values())
