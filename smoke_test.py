"""Offline smoke test: no API key, no network. Run with the venv python."""
import asyncio
import json
import os

os.environ["LYRICS_MCP_RPM"] = "2"

from lyrics_mcp import server

# 1. parse_result across the shapes the model actually produces
cases = {
    "both markers": "TITLE: Night Bus\nLYRICS:\n[Verse]\nglass and rain\n",
    "no LYRICS marker": "TITLE: Night Bus\n\n[Verse]\nglass and rain\n",
    "no TITLE marker": "LYRICS:\n[Verse]\nglass and rain\n",
    "bare text": "[Verse]\nglass and rain\n",
    "fullwidth colon": "TITLE：夜巴\nLYRICS：\n[Verse]\n玻璃与雨\n",
}
for name, raw in cases.items():
    out = server.parse_result(raw)
    assert out["lyrics"].startswith("[Verse]"), (name, out)
    assert out["title"], (name, out)
    # a section tag is never an acceptable title
    assert not out["title"].strip("[]").lower().startswith("verse"), (name, out)
    print(f"  parse_result / {name:18} -> title={out['title'].encode('unicode_escape').decode()!r}")

# 2. prompt assembly
p = server.build_prompt("Brief: late shift walk home\nGenre: indie folk", "Chinese")
assert "late shift walk home" in p and "strictly in this language: Chinese" in p
assert "TITLE: <a short song title>" in p
print(f"  build_prompt                      -> {len(p)} chars, language pinned")

p2 = server.build_prompt("Brief: x", "")
assert "language implied by the brief" in p2
print("  build_prompt (no language)        -> falls back to brief language")

# 3. rate limiter: capacity 2, third call must raise
limiter = server.RateLimiter(2)
limiter.acquire()
limiter.acquire()
try:
    limiter.acquire()
    raise SystemExit("FAIL: rate limiter did not trip")
except RuntimeError as exc:
    print(f"  RateLimiter                       -> tripped: {exc}")

# 4. empty brief is rejected before any API call
try:
    server.write_lyrics(brief="   ")
    raise SystemExit("FAIL: empty brief accepted")
except ValueError as exc:
    print(f"  write_lyrics('')                  -> rejected: {exc}")

# 5. the tool is registered and its schema is what clients will see
tools = asyncio.run(server.mcp.list_tools())
tool = next(t for t in tools if t.name == "write_lyrics")
schema = tool.input_schema
print(f"  registered tools                  -> {[t.name for t in tools]}")
print(f"  required args                     -> {schema.get('required')}")
print(f"  all args                          -> {list(schema['properties'])}")
assert schema.get("required") == ["brief"], schema
assert set(schema["properties"]) == {"brief", "language", "genre", "mood", "structure"}
assert tool.description and "section tags" in tool.description

print("\nALL OFFLINE CHECKS PASSED")
print(json.dumps({"model": server.MODEL, "fallback": server.FALLBACK_MODEL}, indent=2))
