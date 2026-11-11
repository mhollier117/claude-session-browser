"""Manual calibration of the v2 suggester against the real Qwen endpoint (not a test; run by hand).

    .venv\\Scripts\\python.exe tools\\suggest_eval.py [-v] [--only N ...]

For each seeded session name it runs the local retrieval (`suggest.prepare`) over the real launcher index, sends the
v2 prompt to the self-hosted Qwen endpoint exactly as `stage_qwen` does, validates the answer with
`suggest.validate_choice`, and prints a scoreboard: valid answer, correct folder, correct kind (PROJECT or PLACE),
latency, and what the local ranking alone would have offered (top candidate, `strong`).

Read-only: the index is opened but never refreshed, nothing is written to suggest.log or the unreachable marker,
and the API key is never printed.
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from launcher import retrieve  # noqa: E402
from launcher.index import Index  # noqa: E402
from launcher.paths import default_env  # noqa: E402
from launcher.suggest import (_current_alias, _parse_json, _qwen_config, default_http, prepare,  # noqa: E402
                              qwen_request, validate_choice)

# (name, accepted answers, note). An accepted answer is (rel, kind, under): rel compared case-insensitively;
# kind PROJECT / PLACE / None (either); under=True also accepts any folder below rel.
CASES = [
    ("Ninja RMM Alert Review", [("work\\ninja", "PLACE", False)], ""),
    ("razer synapse app reverse engineer", [("home", "PLACE", False)], ""),
    ("razer app decompile", [("home", "PLACE", False)], ""),
    ("local ai transcription pipeline", [("home\\ai", "PLACE", False)], ""),
    ("fortigate vpn cert renewal", [("work\\net", "PLACE", False), ("work\\net\\cert issue", "PROJECT", False)], ""),
    ("bifrost vault key rotation runbook", [("work\\clients\\bast\\bifrost", "PROJECT", False)], ""),
    ("Mediko new hire workflow fix", [("work\\wf\\mediko_user_creation", None, True),
                                      ("work\\clients\\Mediko", None, True)], ""),
    ("Halo ticket list sync bug", [("work\\halo\\view_management", "PROJECT", False)], ""),
    ("CIPP standards drift report", [("work\\cipp", "PLACE", False), ("work\\cipp\\Cipp Standards", "PROJECT", False)],
     ""),
    ("Keeper SSO rollout script", [("work\\clients\\bast\\Keeper", "PROJECT", False)], ""),
    ("homelab pxe boot order", [("home\\homelab\\pxe", "PROJECT", False)], ""),
    ("claude session browser launcher review",
     [("cc\\claude_improvements\\claude-session-browser-main", "PROJECT", False)], ""),
    ("brandito offboarding shared mailbox", [("work\\wf\\Rewst_Master_v3_Client_Offboarding_Emplo", None, "prefix"),
                                             ("work\\wf\\completion_handler_Brandito_offboarding", None, False),
                                             ("work\\clients\\brandito", None, True)], ""),
    ("contact sync duplicate users", [("work\\halo\\contact_sync", "PROJECT", False)], ""),
    ("SFDSDF", [("", "PLACE", True)], "any PLACE, confidence low"),
]


def _rel(env, path) -> str:
    rel = str(Path(path).relative_to(env.root))
    return "" if rel == "." else rel


def _accepts(accepted, rel: str, kind: str | None) -> tuple[bool, bool]:
    """(folder correct, folder and kind correct)."""
    folder_ok = kind_ok = False
    low = rel.lower()
    for want, want_kind, under in accepted:
        w = want.lower()
        if under == "prefix":
            hit = low.startswith(w)
        elif under:
            hit = w == "" or low == w or low.startswith(w + "\\")
        else:
            hit = low == w
        if hit:
            folder_ok = True
            if want_kind is None or want_kind == kind:
                kind_ok = True
    return folder_ok, kind_ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", "--verbose", action="store_true", help="print each user message and raw answer")
    ap.add_argument("--only", type=int, nargs="*", help="1-based case numbers to run")
    args = ap.parse_args()

    env = default_env()
    index = Index(env)  # read-only use: never refresh() here
    rows = index.rows()
    key, base, configured = _qwen_config(env)
    headers = {"Authorization": f"Bearer {key}"}
    status, data = default_http("GET", base + "/models", headers, None, 5)
    model = (_current_alias(data) if status == 200 else None) or configured
    cands = retrieve.candidates(env, None, rows)
    print(f"model={model}  folders={len(cands)}  sessions={len(rows)}  "
          f"strong: RARE_IDF={retrieve.RARE_IDF} STRONG_SUM={retrieve.STRONG_SUM} LEAD={retrieve.LEAD}\n")

    results = []
    for i, (name, accepted, note) in enumerate(CASES, 1):
        if args.only and i not in args.only:
            continue
        prep = prepare(env, rows, name)
        top = prep.scored[0] if prep.scored else None
        is_strong = retrieve.strong(prep.scored)
        t0 = time.monotonic()
        err, raw_text, usage = None, None, None
        try:
            status, data = default_http("POST", base + "/chat/completions", headers,
                                        qwen_request(model, prep.message), 30)
            if status != 200:
                err = f"HTTP {status}"
            else:
                raw_text = data["choices"][0]["message"]["content"]
                usage = (data.get("usage") or {}).get("prompt_tokens")
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
        ms = round((time.monotonic() - t0) * 1000)
        s = None
        if raw_text is not None:
            raw = _parse_json(raw_text)
            s = validate_choice(env, raw, prep, "qwen") if raw is not None else None
        kind = None
        if s is not None:
            kind = next(c.kind for c in prep.choices if c.path == s.path)
        rel = _rel(env, s.path) if s else ""
        folder_ok, kind_ok = _accepts(accepted, rel, kind) if s else (False, False)
        if name == "SFDSDF" and s is not None:
            kind_ok = kind_ok and s.confidence == "low"
        local_ok = top is not None and _accepts(accepted, top.cand.rel, "PROJECT")[1]
        results.append({"name": name, "valid": s is not None, "folder": folder_ok, "correct": kind_ok, "ms": ms,
                        "strong": is_strong, "local_ok": local_ok})
        mark = "OK " if kind_ok else ("~  " if folder_ok else "XX ")
        print(f"{i:2}. {mark}{name}")
        if s is not None:
            print(f"      qwen  {ms:5} ms {usage} tok -> {kind} {rel}" + (f" + {s.new_folder}" if s.new_folder else "")
                  + f"  [{s.confidence}] {s.reason[:110]}")
        else:
            print(f"      qwen  {ms:5} ms INVALID {err or ''} raw={str(raw_text)[:160]!r}")
        if top is not None:
            print(f"      local top={top.cand.rel} score={top.score:.2f} matched={','.join(top.matched)} "
                  f"strong={is_strong} (top {'right' if local_ok else 'wrong'})")
        else:
            print("      local: nothing scores (root)")
        if note:
            print(f"      expected: {note}")
        if args.verbose:
            print("      ---- user message ----")
            print("      " + prep.message.replace("\n", "\n      "))
            print(f"      ---- raw answer ----\n      {raw_text}")
    index.close()

    n = len(results)
    lat = [r["ms"] for r in results]
    strong_rows = [r for r in results if r["strong"]]
    print("\nSCOREBOARD")
    print(f"  valid          {sum(r['valid'] for r in results)}/{n}")
    print(f"  correct folder {sum(r['folder'] for r in results)}/{n}")
    print(f"  correct (folder and kind) {sum(r['correct'] for r in results)}/{n}")
    print(f"  latency median {statistics.median(lat):.0f} ms, max {max(lat)} ms")
    print(f"  local strong   {len(strong_rows)} cases, top right in {sum(r['local_ok'] for r in strong_rows)}; "
          f"weak but top right in {sum(r['local_ok'] for r in results if not r['strong'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
