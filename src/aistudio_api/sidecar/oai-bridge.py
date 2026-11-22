#!/usr/bin/env python3
"""OpenAI chat/completions -> Gemini generateContent. Python 3 stdlib only."""
import json, os, threading, time, uuid, urllib.request, urllib.error
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

UPSTREAM = os.environ.get("OAI_BRIDGE_UPSTREAM", "http://127.0.0.1:8080").rstrip("/")
HOST = os.environ.get("OAI_BRIDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("OAI_BRIDGE_PORT", "8765"))
KEEPALIVE = os.environ.get("OAI_BRIDGE_KEEPALIVE", "1") != "0"
SAFETY = [{"category": c, "threshold": "BLOCK_NONE"} for c in (
    "HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
    "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT")]
FINISH = {"STOP": "stop", "MAX_TOKENS": "length", "SAFETY": "content_filter",
          "PROHIBITED_CONTENT": "content_filter", "BLOCKLIST": "content_filter",
          "RECITATION": "content_filter", "SPII": "content_filter"}
# upstream is local; a Clash http_proxy in the shell would otherwise swallow these requests
DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))
# "<model>[防截断]" asks Gemini to hand the reply over as a function-call argument instead of plain text
TOOL_SUFFIX = "[防截断]"
REPLY_TOOL = {"name": "reply", "description": "Deliver the complete reply to the user.",
              "parameters": {"type": "object", "required": ["text"],
                             "properties": {"text": {"type": "string", "description": "The full reply, nothing omitted."}}}}
REPLY_RULE = "Deliver your entire reply by calling reply(text=...). Put the complete reply in text; write nothing outside the call."
REPLY_RULE_TOOLS = ("Whenever you write to the user, deliver that entire text by calling reply(text=...) and write nothing "
                    "outside the call. The other tools stay available; call them as usual whenever they are needed.")


def to_parts(content):
    if content is None:
        return []
    if isinstance(content, str):
        return [{"text": content}] if content else []
    parts = []
    for p in content:
        if p.get("type") == "text" and p.get("text"):
            parts.append({"text": p["text"]})
        elif p.get("type") == "image_url":
            url = (p.get("image_url") or {}).get("url", "")
            if url.startswith("data:") and ";base64," in url:
                mime, data = url[5:].split(";base64,", 1)
                parts.append({"inlineData": {"mimeType": mime, "data": data}})
    return parts


SCHEMA_KEYS = {"type", "format", "description", "nullable", "enum", "properties", "required", "items",
               "minItems", "maxItems", "minimum", "maximum", "minLength", "maxLength", "pattern", "anyOf", "title"}
# Gemini 3+ wants the signature (and call id) it issued back when a function call is replayed. OpenAI clients only
# echo our tool_call id, so keep both here by that id; calls we never saw get the documented bypass instead.
ISSUED = {}
SKIP_SIGNATURE = "skip_thought_signature_validator"


def clean_schema(x):
    if isinstance(x, list):
        return [clean_schema(i) for i in x]
    if not isinstance(x, dict):
        return x
    out = {}
    for k, v in x.items():
        if k == "properties" and isinstance(v, dict):
            out[k] = {n: clean_schema(sub) for n, sub in v.items()}
        elif k in SCHEMA_KEYS:
            out[k] = clean_schema(v)
    if isinstance(out.get("type"), list):
        types = [t for t in out["type"] if t != "null"]
        out["type"] = types[0] if types else "string"
        if len(types) < len(x["type"]):
            out["nullable"] = True
    return out


def to_tools(body):
    decls = []
    for t in body.get("tools") or []:
        f = t.get("function") if t.get("type", "function") == "function" else None
        if not f or not f.get("name"):
            continue
        d = {"name": f["name"], "description": f.get("description", "")}
        # Gemini rejects an OBJECT schema without properties; a tool that takes nothing just omits parameters
        if (f.get("parameters") or {}).get("properties"):
            d["parameters"] = clean_schema(f["parameters"])
        decls.append(d)
    if not decls:
        return None, None
    choice, mode = body.get("tool_choice"), {"mode": "AUTO"}
    if choice == "none":
        mode = {"mode": "NONE"}
    elif choice == "required":
        mode = {"mode": "ANY"}
    elif isinstance(choice, dict) and (choice.get("function") or {}).get("name"):
        mode = {"mode": "ANY", "allowedFunctionNames": [choice["function"]["name"]]}
    return [{"functionDeclarations": decls}], {"functionCallingConfig": mode}


def parse_args(raw):
    if isinstance(raw, dict):
        return raw
    try:
        v = json.loads(raw or "{}")
        return v if isinstance(v, dict) else {"value": v}
    except ValueError:
        return {"value": raw}


def to_gemini(body):
    system, contents, names = [], [], {}
    for m in body.get("messages", []):
        role, parts = m.get("role"), to_parts(m.get("content"))
        if role == "assistant":
            for i, c in enumerate(m.get("tool_calls") or []):
                f = c.get("function") or {}
                known = c.get("id") in ISSUED
                sig, gid = ISSUED.get(c.get("id"), (None, None))
                names[c.get("id")] = (f.get("name", ""), gid)
                call = {"name": f.get("name", ""), "args": parse_args(f.get("arguments"))}
                if gid:
                    call["id"] = gid
                part = {"functionCall": call}
                # only the first call of a step carries a signature
                if sig or (i == 0 and not known):
                    part["thoughtSignature"] = sig or SKIP_SIGNATURE
                parts.append(part)
        elif role in ("tool", "function"):
            raw = m.get("content")
            if isinstance(raw, list):
                raw = "".join(p.get("text", "") for p in raw if isinstance(p, dict))
            try:
                result = json.loads(raw) if isinstance(raw, str) else raw
            except ValueError:
                result = raw
            name, gid = names.get(m.get("tool_call_id"), ("", None))
            fr = {"name": m.get("name") or name, "response": result if isinstance(result, dict) else {"result": result}}
            if gid:
                fr["id"] = gid
            parts = [{"functionResponse": fr}]
        if not parts:
            continue
        if role in ("system", "developer"):
            system.extend(parts)
            continue
        g_role = "model" if role == "assistant" else "user"
        if contents and contents[-1]["role"] == g_role:
            contents[-1]["parts"].extend(parts)
        else:
            contents.append({"role": g_role, "parts": parts})
    if not contents:
        contents, system = [{"role": "user", "parts": system or [{"text": "."}]}], []
    if contents[-1]["role"] == "model":
        # Gemini rejects assistant prefill ("Requests ending with a model turn are not supported")
        contents.append({"role": "user", "parts": [{"text": "Continue."}]})
    req = {"contents": contents, "safetySettings": SAFETY}
    if system:
        req["systemInstruction"] = {"parts": system}
    cfg = {}
    for o, g in (("temperature", "temperature"), ("top_p", "topP"), ("top_k", "topK")):
        if body.get(o) is not None:
            cfg[g] = body[o]
    mt = body.get("max_completion_tokens") or body.get("max_tokens")
    if mt:
        cfg["maxOutputTokens"] = mt
    stop = body.get("stop")
    if stop:
        cfg["stopSequences"] = [stop] if isinstance(stop, str) else stop
    effort = str(body.get("reasoning_effort") or "").upper()
    if effort in ("MINIMAL", "LOW", "MEDIUM", "HIGH"):
        cfg["thinkingConfig"] = {"includeThoughts": True, "thinkingLevel": effort}
    if cfg:
        req["generationConfig"] = cfg
    tools, tool_cfg = to_tools(body)
    if tools:
        req["tools"], req["toolConfig"] = tools, tool_cfg
    return req


def tool_mode(model):
    return (model.removesuffix(TOOL_SUFFIX), True) if model.endswith(TOOL_SUFFIX) else (model, False)


def with_reply_tool(req, choice):
    """Add reply() for [防截断]. The caller's own tools stay callable beside it. False = request left untouched."""
    own = [] if choice == "none" else [d for t in req.get("tools", []) for d in t["functionDeclarations"]]
    if own and (choice not in (None, "auto") or any(d["name"] == "reply" for d in own)):
        return False  # the caller forces one of its own tools, or already owns the name
    mode = {"mode": "ANY"} if own else {"mode": "ANY", "allowedFunctionNames": ["reply"]}
    req["tools"] = [{"functionDeclarations": own + [REPLY_TOOL]}]
    req["toolConfig"] = {"functionCallingConfig": mode}
    req.setdefault("systemInstruction", {"parts": []})["parts"].append({"text": REPLY_RULE_TOOLS if own else REPLY_RULE})
    return True


def from_gemini(g, reply_mode=False):
    cands = g.get("candidates") or []
    if not cands:
        reason = (g.get("promptFeedback") or {}).get("blockReason", "")
        return "", "", "content_filter" if reason else "stop", []
    c = cands[0]
    text, thought, calls, loose_sig = [], [], [], None
    for p in (c.get("content") or {}).get("parts", []):
        if p.get("thought"):
            thought.append(p.get("text", ""))
        elif isinstance(p.get("functionCall"), dict):
            fc = p["functionCall"]
            args = fc.get("args") or {}
            sig, loose_sig = p.get("thoughtSignature") or loose_sig, None
            if reply_mode and fc.get("name") == "reply":
                text.append(args.get("text", "") if isinstance(args, dict) else str(args))
            else:
                cid = "call_" + uuid.uuid4().hex[:24]
                if len(ISSUED) > 2000:
                    ISSUED.clear()
                ISSUED[cid] = (sig, fc.get("id"))
                calls.append({"id": cid, "type": "function",
                              "function": {"name": fc.get("name", ""), "arguments": json.dumps(args, ensure_ascii=False)}})
        elif "text" in p:
            text.append(p["text"] or "")
        else:
            # the signature can travel as a part of its own just ahead of the call it belongs to
            loose_sig = p.get("thoughtSignature") or loose_sig
    finish = "tool_calls" if calls else FINISH.get(c.get("finishReason", "STOP"), "stop")
    return "".join(text), "".join(thought), finish, calls


def usage(g):
    u = g.get("usageMetadata") or {}
    p, c = u.get("promptTokenCount", 0), u.get("candidatesTokenCount", 0)
    return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": u.get("totalTokenCount", p + c)}


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def key(self):
        auth = self.headers.get("Authorization", "")
        return auth[7:].strip() if auth.lower().startswith("bearer ") else self.headers.get("x-goog-api-key", "")

    def call(self, method, path, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        r = urllib.request.Request(UPSTREAM + path, data=data, method=method,
                                   headers={"Content-Type": "application/json", "x-goog-api-key": self.key()})
        try:
            with DIRECT.open(r, timeout=600) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, {"error": {"message": raw.decode("utf-8", "replace")}}
        except (urllib.error.URLError, OSError) as e:
            return 502, {"error": {"message": f"upstream {UPSTREAM} unreachable: {e}"}}

    def send(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(b)

    def err(self, code, g):
        e = g.get("error") if isinstance(g.get("error"), dict) else {"message": json.dumps(g, ensure_ascii=False)}
        self.send(code, {"error": {"message": e.get("message", ""), "type": "upstream_error", "code": code}})

    def do_OPTIONS(self):
        self.send_response(204)
        # "*" in Allow-Headers does not cover Authorization, so echo what the browser asked for
        asked = self.headers.get("Access-Control-Request-Headers") or "Authorization, Content-Type"
        for k, v in (("Access-Control-Allow-Origin", "*"), ("Access-Control-Allow-Headers", asked),
                     ("Access-Control-Allow-Methods", "GET, POST, OPTIONS"),
                     ("Access-Control-Allow-Private-Network", "true"), ("Content-Length", "0")):
            self.send_header(k, v)
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        if path in ("/v1/models", "/models"):
            code, g = self.call("GET", "/v1beta/models")
            if code != 200:
                return self.err(code, g)
            ids = [m.get("name", "").removeprefix("models/") for m in g.get("models", [])]
            ids = [i for i in ids if i]
            return self.send(200, {"object": "list", "data": [
                {"id": i, "object": "model", "created": 0, "owned_by": "google"}
                for i in ids + [i + TOOL_SUFFIX for i in ids]]})
        if path in ("", "/health"):
            return self.send(200, {"ok": True, "upstream": UPSTREAM})
        self.send(404, {"error": {"message": "not found"}})

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        if path not in ("/v1/chat/completions", "/chat/completions"):
            return self.send(404, {"error": {"message": "not found"}})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except ValueError:
            return self.send(400, {"error": {"message": "bad json"}})
        model, tool = tool_mode(str(body.get("model") or "gemini-2.5-flash").removeprefix("models/"))
        payload = to_gemini(body)
        reply_mode = tool and with_reply_tool(payload, body.get("tool_choice"))
        if body.get("stream"):
            return self.stream(model, payload, reply_mode)
        code, g = self.call("POST", f"/v1beta/models/{model}:generateContent", payload)
        if code != 200:
            return self.err(code, g)
        text, thought, finish, calls = from_gemini(g, reply_mode)
        msg = {"role": "assistant", "content": text or (None if calls else "")}
        if calls:
            msg["tool_calls"] = calls
        if thought:
            msg["reasoning_content"] = thought
        self.send(200, {"id": "chatcmpl-" + uuid.uuid4().hex[:24], "object": "chat.completion",
                        "created": int(time.time()), "model": model,
                        "choices": [{"index": 0, "message": msg, "finish_reason": finish}], "usage": usage(g)})

    def stream(self, model, payload, reply_mode=False):
        r = urllib.request.Request(f"{UPSTREAM}/v1beta/models/{model}:streamGenerateContent?alt=sse",
                                   data=json.dumps(payload).encode(), method="POST",
                                   headers={"Content-Type": "application/json", "x-goog-api-key": self.key()})
        base = {"id": "chatcmpl-" + uuid.uuid4().hex[:24], "object": "chat.completion.chunk",
                "created": int(time.time()), "model": model}
        lock, done, t0, last = threading.Lock(), threading.Event(), time.monotonic(), [0.0]

        def chunk(delta, fin=None):
            return json.dumps(dict(base, choices=[{"index": 0, "delta": delta, "finish_reason": fin}]), ensure_ascii=False)

        def put(data):
            if not last[0]:
                self.send_response(200)
                for k, v in (("Content-Type", "text/event-stream; charset=utf-8"), ("Cache-Control", "no-cache"),
                             ("Connection", "close"), ("Access-Control-Allow-Origin", "*")):
                    self.send_header(k, v)
                self.end_headers()
                self.close_connection = True
                last[0] = time.monotonic()
                put(chunk({"role": "assistant", "content": ""}))
            self.wfile.write(f"data: {data}\n\n".encode())
            self.wfile.flush()
            last[0] = time.monotonic()

        def emit(delta, fin=None):
            with lock:
                put(chunk(delta, fin))

        def fail(code, g):
            with lock:
                done.set()
                if not last[0]:
                    return self.err(code, g)
                e = g.get("error") if isinstance(g.get("error"), dict) else {"message": json.dumps(g, ensure_ascii=False)}
                put(json.dumps({"error": {"message": e.get("message", ""), "type": "upstream_error", "code": code}},
                               ensure_ascii=False))
                put("[DONE]")

        def tick():
            # Gemini can stay silent until it is finished (it does under [防截断]); a client's timer only moves on data events
            while not done.wait(0.5):
                with lock:
                    if done.is_set():
                        return
                    if time.monotonic() - (last[0] or t0) >= 1:
                        try:
                            put(chunk({"content": ""}))
                        except OSError:
                            return

        if KEEPALIVE:
            threading.Thread(target=tick, daemon=True).start()
        try:
            try:
                resp = DIRECT.open(r, timeout=600)
            except urllib.error.HTTPError as e:
                raw = e.read()
                try:
                    return fail(e.code, json.loads(raw))
                except ValueError:
                    return fail(e.code, {"error": {"message": raw.decode("utf-8", "replace")}})
            except (urllib.error.URLError, OSError) as e:
                return fail(502, {"error": {"message": f"upstream {UPSTREAM} unreachable: {e}"}})
            finish, n_calls = None, 0
            with resp:
                for line in resp:
                    line = line.strip()
                    if not line.startswith(b"data:"):
                        continue
                    g = json.loads(line[5:].strip() or b"{}")
                    if isinstance(g.get("error"), dict):
                        emit({"content": f"[error] {g['error'].get('message', '')}"})
                        finish = "stop"
                        break
                    text, thought, fin, calls = from_gemini(g, reply_mode)
                    if thought:
                        emit({"reasoning_content": thought})
                    if text:
                        emit({"content": text})
                    for c in calls:
                        emit({"tool_calls": [dict(c, index=n_calls)]})
                        n_calls += 1
                    if (g.get("candidates") or [{}])[0].get("finishReason"):
                        finish = fin
            with lock:
                done.set()
                put(chunk({}, "tool_calls" if n_calls else (finish or "stop")))
                put("[DONE]")
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            done.set()

    def log_message(self, fmt, *args):
        print(time.strftime("%H:%M:%S"), self.command, self.path, *args[1:2], flush=True)


if __name__ == "__main__":
    print(f"oai-bridge on http://{HOST}:{PORT}/v1 -> {UPSTREAM}", flush=True)
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
