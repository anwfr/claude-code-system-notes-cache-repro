"""Prompt cache test: the same conversation three times; only the system notes and the marker change.

Run:  API_KEY=<Cheaper Inference key> python3 repro.py cheaperinference
      API_KEY=<Anthropic API key> python3 repro.py anthropic
      add SESSION_HEADER=1 to send Cheaper Inference's x-ci-prompt-cache-session header
Needs Python 3 only. Costs about 0.70 USD on Cheaper Inference. Saves every
request (exact body) and response (status, headers, body) in ./requests/
"""
import json, os, pathlib, sys, time, urllib.error, urllib.request, uuid

URLS = {"cheaperinference": "https://api.cheaperinference.com/v1/messages",
        "anthropic": "https://api.anthropic.com/v1/messages"}
PROVIDER = sys.argv[1] if len(sys.argv) > 1 else ""
if PROVIDER not in URLS:
    sys.exit("usage: python3 repro.py cheaperinference|anthropic")
MODEL = "claude-sonnet-5"
TURNS = 4
KEY = os.environ.get("API_KEY") or os.environ["ANTHROPIC_API_KEY"]  # the key of the chosen provider
# Claude Code announces this beta to send system notes in the middle of the conversation.
BETA = "mid-conversation-system-2026-04-07"
CACHE = {"type": "ephemeral"}
LONG = " ".join("Line %d: the quick brown fox jumps over the lazy dog." % i for i in range(600))
OUT = pathlib.Path("requests")
OUT.mkdir(exist_ok=True)
TITLES = {"user": "RUN 1 - normal conversation, no system notes: the marker is on the last user message",
          "system": "RUN 2 - same conversation, plus a short system note after each user message\n"
                    "        (like Claude Code): the marker is on the last system note",
          "notes-user": "RUN 3 - same as RUN 2, but the marker is on the last user message, before the note\n"
                        "        (like Claude Code with CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1)"}


def request(last_role, turn, run):
    messages = []
    for i in range(1, turn + 1):
        messages.append({"role": "user", "content": [{"type": "text", "text": "Question %d. %s" % (i, LONG)}]})
        if last_role != "user":  # what Claude Code does: a short system note after each user message
            messages.append({"role": "system", "content": [{"type": "text", "text": "Reminder %d: answer ok." % i}]})
        if i < turn:
            messages.append({"role": "assistant", "content": [{"type": "text", "text": "ok"}]})
    marked = messages[-2] if last_role == "notes-user" else messages[-1]
    marked["content"][-1]["cache_control"] = CACHE
    return {"model": MODEL, "max_tokens": 5,
            "system": [{"type": "text", "text": "Run %s. Answer ok. %s" % (run, LONG), "cache_control": CACHE}],
            "messages": messages}


print("Provider: %s, model %s" % (PROVIDER, MODEL))
for last_role in ["user", "system", "notes-user"]:
    run = uuid.uuid4().hex[:8]  # new text each run, so nothing is cached at the start
    print("\n" + TITLES[last_role])
    previous_total = None
    for turn in range(1, TURNS + 1):
        body = request(last_role, turn, run)
        name = "%s-turn%d" % (last_role, turn)
        (OUT / (name + ".request.json")).write_text(json.dumps(body, indent=1))
        headers = {"anthropic-version": "2023-06-01", "anthropic-beta": BETA, "content-type": "application/json"}
        if os.environ.get("SESSION_HEADER"):  # Cheaper Inference's documented routing affinity, one value per pass
            headers["x-ci-prompt-cache-session"] = "repro-%s-%s" % (run, last_role)
        req = urllib.request.Request(URLS[PROVIDER], json.dumps(body).encode(), dict(headers, **{"x-api-key": KEY}))
        # Everything on the wire except the key: request headers, response status, headers and body.
        exchange = {"url": URLS[PROVIDER], "request_headers": dict(headers, **{"x-api-key": "<redacted>"}),
                    "request_body_file": name + ".request.json"}
        try:
            for attempt in range(3):  # a connection that times out before any answer is sent again, as is
                try:
                    r = urllib.request.urlopen(req, timeout=120)
                    break
                except urllib.error.HTTPError:
                    raise
                except (urllib.error.URLError, OSError) as e:
                    print("  turn %d: network error (%s), trying again" % (turn, e))
                    time.sleep(10)
            else:
                sys.exit("  turn %d: no connection after 3 attempts" % turn)
            with r:
                answer = json.load(r)
                exchange.update(status=r.status, response_headers=dict(r.headers.items()), response_body=answer)
                request_id = r.headers.get("x-ci-request-id") or r.headers.get("request-id")
        except urllib.error.HTTPError as e:
            error = e.read().decode("utf-8", "replace")
            exchange.update(status=e.code, response_headers=dict(e.headers.items()), response_body=error)
            (OUT / (name + ".response.json")).write_text(json.dumps(exchange, indent=1))
            print("  turn %d: refused, HTTP %d: %s" % (turn, e.code, error[:300]))
            break
        (OUT / (name + ".response.json")).write_text(json.dumps(exchange, indent=1))
        u = answer["usage"]
        read = u.get("cache_read_input_tokens", 0)
        total = read + u.get("cache_creation_input_tokens", 0) + u["input_tokens"]
        line = "  turn %d: %6d tokens sent, %6d read from cache" % (turn, total, read)
        if previous_total:
            line += "  = %3d%% of the previous turn reused" % round(100 * read / previous_total)
        print(line + "\n          request id %s   message id %s" % (request_id, answer.get("id")))
        previous_total = total
    print("  expected on turns 2 to %d: 100%%" % TURNS)
