#!/usr/bin/env python3
"""Build the graphiti-extraction distillation dataset from harvested pairs.

Sources: training-pairs-ox.jsonl + training-pairs-qwen.jsonl (bulk teachers).
Luna/terra slices and the live nodes-only file are excluded: luna's superset
depth is a different output style, and rows without `rich` lost the typed
entities/summaries the student must learn.

Target = the teacher's `rich` object re-serialized to the exact schema the
harvest PROMPT specifies. The user turn is the harvest PROMPT verbatim, so a
trained student is a drop-in replacement at the same callsites.

Output: data/train.jsonl, data/valid.jsonl, data/test.jsonl in mlx-lm
messages format, plus data/BUILD-REPORT.json with kept/dropped counts.
"""
import os
import hashlib
import json
import random
from pathlib import Path

SRC = Path(os.path.expanduser("~/Library/Memory/graphiti/data"))
OUT = Path(os.path.expanduser("~/Library/Memory/graphiti/training/data"))
FILES = ["training-pairs-qwen.jsonl", "training-pairs-ox.jsonl"]  # qwen first: preferred on dupes
MAX_CHARS = 14000   # prompt+target ceiling, keeps sequences inside 4096 tokens
VALID_N = 400
TEST_N = 400

PROMPT = """Extract a knowledge graph from the notes below. Output ONLY valid JSON, no prose, no code fences.

Schema:
{"entities":[{"name":"...","type":"...","summary":"..."}],
 "relations":[{"source":"...","relation":"...","target":"...","fact":"one plain sentence","date":"YYYY-MM-DD or null"}]}

Rules: entities are real things (people, machines, systems, companies, projects, tools). Relations state what the notes literally say — no inference beyond the text. "fact" is one short sentence. Use the note date for "date" when the fact is time-anchored, else null.

NOTES (dated {date}):
{body}"""


def canonical_target(rich):
    if not isinstance(rich, dict):
        return None
    ents = rich.get("entities")
    rels = rich.get("relations")
    if not isinstance(ents, list) or not isinstance(rels, list) or not ents or not rels:
        return None
    out_e, out_r = [], []
    for e in ents:
        if not isinstance(e, dict) or not e.get("name"):
            return None
        out_e.append({"name": str(e.get("name")), "type": str(e.get("type") or ""),
                      "summary": str(e.get("summary") or "")})
    for r in rels:
        if not isinstance(r, dict) or not r.get("source") or not r.get("target"):
            return None
        date = r.get("date")
        out_r.append({"source": str(r.get("source")), "relation": str(r.get("relation") or ""),
                      "target": str(r.get("target")), "fact": str(r.get("fact") or ""),
                      "date": date if isinstance(date, str) and date else None})
    return json.dumps({"entities": out_e, "relations": out_r}, ensure_ascii=False)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    seen = {}
    stats = {f: {"read": 0, "no_rich": 0, "bad_rich": 0, "too_long": 0, "dup": 0, "kept": 0} for f in FILES}
    for fname in FILES:
        st = stats[fname]
        with (SRC / fname).open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                st["read"] += 1
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    st["bad_rich"] += 1
                    continue
                rich = rec.get("rich")
                if rich is None:
                    st["no_rich"] += 1
                    continue
                target = canonical_target(rich)
                if target is None:
                    st["bad_rich"] += 1
                    continue
                body = str(rec.get("input") or "").strip()
                if not body:
                    st["bad_rich"] += 1
                    continue
                prompt = PROMPT.replace("{date}", str(rec.get("date") or "unknown")).replace("{body}", body)
                if len(prompt) + len(target) > MAX_CHARS:
                    st["too_long"] += 1
                    continue
                key = hashlib.sha256(body.encode()).hexdigest()
                if key in seen:
                    st["dup"] += 1
                    continue
                seen[key] = {"messages": [
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": target},
                ]}
                st["kept"] += 1

    rows = list(seen.values())
    random.Random(13).shuffle(rows)
    test, valid, train = rows[:TEST_N], rows[TEST_N:TEST_N + VALID_N], rows[TEST_N + VALID_N:]
    for name, part in (("train", train), ("valid", valid), ("test", test)):
        with (OUT / f"{name}.jsonl").open("w") as fh:
            for row in part:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    report = {"per_file": stats, "total_kept": len(rows),
              "train": len(train), "valid": len(valid), "test": len(test)}
    (OUT / "BUILD-REPORT.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
