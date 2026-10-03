"""The same small Claude Code task, run on Anthropic or on Cheaper Inference.

Claude Code reads four files, one per request (each file names the next one),
so the conversation grows. A local relay between Claude Code and the provider
records every exchange as it passes.

Run:
  python3 claude-code-compare.py anthropic <out-dir>          # your normal Claude Code login
  CI_API_KEY=<Cheaper Inference key> python3 claude-code-compare.py cheaperinference <out-dir>
  options: --no-betas      runs Claude Code with CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1
                           (the marker then goes on the last user message)
           --reminder-off  runs Claude Code with CLAUDE_CODE_TOTAL_TOKENS_REMINDER=off
                           (no "<total_tokens>" note added at each request)
  python3 claude-code-compare.py --report <out-dir>           # rebuild output.txt from the saved traffic
Needs Python 3 and the `claude` command. Writes into <out-dir>:
  output.txt                 one line per request: tokens, cache read, request id, message id
  run.json                   provider, model, Claude Code version
  requests/NN.request.json   the body Claude Code sent (redactions listed in "redacted")
  requests/NN.response.json  URL, request headers (auth hidden), status, response headers, raw response
"""
import http.client, json, os, pathlib, re, subprocess, sys, tempfile, threading, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = "claude-sonnet-5"
UPSTREAMS = {"anthropic": "api.anthropic.com", "cheaperinference": "api.cheaperinference.com"}
TASK = ("Read start.txt with the Read tool. Its last line names the next file to read. "
        "Keep reading file after file until a file ends with END, then answer with the single word ok.")
SECRET_HEADERS = {"authorization", "x-api-key", "cookie", "set-cookie",
                  "anthropic-organization-id", "anthropic-workspace-id", "x-claude-code-session-id"}
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
HOP = {"host", "content-length", "connection", "transfer-encoding", "accept-encoding", "keep-alive"}
# Personal text that Claude Code copies into its prompt (your own instruction files) is
# replaced by its length in the saved copies; nothing else is changed.
PERSONAL = [t.strip() for t in (p.read_text() for p in (pathlib.Path.home() / ".claude").glob("*.md")) if t.strip()]


def redact(node, found):
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k == "metadata":
                out[k] = "<redacted: device and session ids>"
                found.add("metadata")
            else:
                out[k] = redact(v, found)
        return out
    if isinstance(node, list):
        return [redact(v, found) for v in node]
    if isinstance(node, str):
        if EMAIL.search(node):
            node = EMAIL.sub("<redacted: email>", node)
            found.add("email address")
        for text in PERSONAL:
            if text in node:
                node = node.replace(text, "<redacted: personal instruction file, %d chars>" % len(text))
                found.add("personal instruction files")
    return node


OPTIONS = {"--no-betas": ("CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS", "1"),
           "--reminder-off": ("CLAUDE_CODE_TOTAL_TOKENS_REMINDER", "off")}


def record(provider, out_dir, options=()):
    """Run Claude Code through the relay; save every exchange under out_dir/requests/."""
    exchanges = []

    class Relay(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("content-length") or 0))
            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
            conn = http.client.HTTPSConnection(UPSTREAMS[provider], timeout=600)
            conn.request(self.command, self.path, body=raw or None,
                         headers=dict(headers, **{"accept-encoding": "identity"}))
            resp = conn.getresponse()
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() not in HOP:
                    self.send_header(k, v)
            self.end_headers()
            chunks = []
            while True:
                chunk = resp.read1(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                self.wfile.write(chunk)
                self.wfile.flush()
            conn.close()
            hide = lambda items: {k: ("<redacted>" if k.lower() in SECRET_HEADERS
                                      or k.lower().startswith("anthropic-ratelimit") else v) for k, v in items}
            exchanges.append({"path": self.path, "raw": raw, "request_headers": hide(headers.items()),
                              "status": resp.status, "response_headers": hide(resp.getheaders()),
                              "response_raw": b"".join(chunks).decode("utf-8", "replace")})

        do_GET = do_POST

    server = ThreadingHTTPServer(("127.0.0.1", 0), Relay)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    env = {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_")}
    settings_env = dict(OPTIONS[o] for o in options)
    env.update(settings_env)
    settings = {"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:%d" % server.server_address[1],
                        "ANTHROPIC_SMALL_FAST_MODEL": MODEL, "DISABLE_TELEMETRY": "1", "DISABLE_AUTOUPDATER": "1"}}
    if provider == "cheaperinference":
        settings["apiKeyHelper"] = "printenv CI_API_KEY"
    with tempfile.TemporaryDirectory() as work:
        names = ["start"] + [uuid.uuid4().hex[:8] for _ in range(3)] + ["END"]
        for name, following in zip(names, names[1:]):  # four files of about 6 000 tokens each
            lines = ["%s line %d: the quick brown fox jumps over the lazy dog." % (name, i) for i in range(500)]
            last = "END" if following == "END" else "Next file: %s.txt" % following
            pathlib.Path(work, name + ".txt").write_text("\n".join(lines + [last]))
        pathlib.Path(work, "settings.json").write_text(json.dumps(settings))
        claude = subprocess.run(["claude", "-p", TASK, "--model", MODEL, "--allowedTools", "Read",
                                 "--strict-mcp-config", "--settings", "settings.json"],
                                cwd=work, env=env, capture_output=True, text=True, timeout=900)
    server.shutdown()
    (out_dir / "requests").mkdir(parents=True, exist_ok=True)
    for n, x in enumerate(exchanges, 1):
        found = set()
        body = redact(json.loads(x["raw"] or b"{}"), found)
        (out_dir / "requests" / ("%02d.request.json" % n)).write_text(
            json.dumps({"redacted": sorted(found), "body": body}, indent=1, ensure_ascii=False))
        (out_dir / "requests" / ("%02d.response.json" % n)).write_text(json.dumps(
            {"url": "https://%s%s" % (UPSTREAMS[provider], x["path"]), "request_headers": x["request_headers"],
             "status": x["status"], "response_headers": x["response_headers"], "response_raw": x["response_raw"]},
            indent=1, ensure_ascii=False))
    version = subprocess.run(["claude", "--version"], capture_output=True, text=True).stdout.strip()
    (out_dir / "run.json").write_text(json.dumps(
        {"provider": provider, "model": MODEL, "claude_code": version, "settings": settings_env, "claude_exit": claude.returncode,
         "claude_error": (claude.stderr or "")[-500:] if claude.returncode else ""}, indent=1))


def usage_and_id(stream):
    """Usage (message_start updated by message_delta) and message id, from the raw response."""
    usage, message_id = {}, None
    for line in stream.splitlines():
        if line.startswith("data:"):
            try:
                event = json.loads(line[5:])
            except ValueError:
                continue
            if event.get("type") == "message_start":
                message_id = (event.get("message") or {}).get("id")
                usage.update((event.get("message") or {}).get("usage") or {})
            elif event.get("type") == "message_delta":
                usage.update({k: v for k, v in (event.get("usage") or {}).items() if v is not None})
    return usage, message_id


def report(out_dir):
    run = json.loads((out_dir / "run.json").read_text())
    settings = run.get("settings") or ({"CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1"} if run.get("no_betas") else {})
    lines = ["Claude Code %s on %s, model %s%s" % (run["claude_code"], run["provider"], run["model"],
                                                   "".join(", with %s=%s" % kv for kv in settings.items())), "",
             "Conversation requests (they carry the tools). Expected from the second one on: 100% reused.", ""]
    side, previous = [], None
    for f in sorted((out_dir / "requests").glob("*.request.json")):
        n = f.name.split(".")[0]
        body = json.loads(f.read_text())["body"]
        response = json.loads((out_dir / "requests" / (n + ".response.json")).read_text())
        h = {k.lower(): v for k, v in response["response_headers"].items()}
        request_id = h.get("x-ci-request-id") or h.get("request-id") or "none"
        endpoint = response["url"].split("/v1/")[-1].split("?")[0]
        if not body.get("tools") or endpoint != "messages":
            side.append("  %s  %s, status %s, request id %s" % (n, endpoint, response["status"], request_id))
            continue
        u, message_id = usage_and_id(response["response_raw"])
        read = u.get("cache_read_input_tokens") or 0
        sent = read + (u.get("cache_creation_input_tokens") or 0) + (u.get("input_tokens") or 0)
        line = "  %s  %6d tokens sent, %6d read from cache" % (n, sent, read)
        if previous:
            line += "  = %3d%% of the previous request reused" % round(100 * read / previous)
        lines += [line, "      request id %s   message id %s   at %s" % (request_id, message_id, h.get("date", "?"))]
        previous = sent
    lines += ["", "Side requests (not part of the conversation):"] + (side or ["  none"])
    if run.get("claude_exit"):
        lines += ["", "claude exited with %s: %s" % (run["claude_exit"], run["claude_error"])]
    (out_dir / "output.txt").write_text("\n".join(lines) + "\n")
    return "\n".join(lines)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--report":
        print(report(pathlib.Path(sys.argv[2])))
    elif len(sys.argv) >= 3 and sys.argv[1] in UPSTREAMS and all(o in OPTIONS for o in sys.argv[3:]):
        record(sys.argv[1], pathlib.Path(sys.argv[2]), options=sys.argv[3:])
        print(report(pathlib.Path(sys.argv[2])))
    else:
        sys.exit("usage: python3 claude-code-compare.py anthropic|cheaperinference <out-dir> [--no-betas] [--reminder-off]\n"
                 "       python3 claude-code-compare.py --report <out-dir>")
