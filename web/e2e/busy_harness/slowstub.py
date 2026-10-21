import json, time, os, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
TPS=float(os.environ.get("TPS","30")); TOK=4
def long_reply():
    parts=[]; i=0
    while sum(map(len,parts))<16000:
        i+=1
        parts.append(f"## Section {i}\n\nThis is paragraph {i} with **bold**, *italics*, `inline code` and a [link](https://example.com). Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor.\n\n- item one of {i}\n- item two of {i}\n\n```python\ndef f{i}(x):\n    # comment {i}\n    return [x * k for k in range({i})]\n```\n\n")
    return "".join(parts)
LONG=long_reply()
class H(BaseHTTPRequestHandler):
    protocol_version="HTTP/1.1"
    def log_message(self,*a): pass
    def j(self,c,o):
        b=json.dumps(o).encode(); self.send_response(c); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        self.j(200,{"object":"list","data":[{"id":"stub","object":"model"}]})
    def do_POST(self):
        n=int(self.headers.get("Content-Length") or 0); req=json.loads(self.rfile.read(n) or b"{}")
        msgs=req.get("messages",[]); last=""
        for m in msgs:
            if m.get("role")=="user":
                c=m.get("content"); last=c if isinstance(c,str) else " ".join(p.get("text","") for p in c if isinstance(p,dict))
        import re
        alltxt=" ".join((m.get("content") if isinstance(m.get("content"),str) else "") for m in msgs if m.get("role")=="user")
        ids=sorted(set(re.findall(r"MSG-[A-Za-z0-9]+",alltxt)))
        tool_last = bool(msgs) and msgs[-1].get("role")=="tool"
        if "TOOLSTEP" in last and not tool_last and "SHORT" not in last:
            sys.stderr.write(f"req TOOL last={last[-60:]!r}\n"); sys.stderr.flush()
            if not req.get("stream"):
                return self.j(200,{"id":"x","object":"chat.completion","model":"stub","choices":[{"index":0,"message":{"role":"assistant","content":"","tool_calls":[{"id":"call_1","type":"function","function":{"name":"terminal","arguments":json.dumps({"command":"sleep 8; echo toolstep-done"})}}]},"finish_reason":"tool_calls"}]})
            self.send_response(200); self.send_header("Content-Type","text/event-stream"); self.send_header("Connection","close"); self.end_headers()
            def ev2(d,f=None): return ("data: "+json.dumps({"id":"x","object":"chat.completion.chunk","model":"stub","choices":[{"index":0,"delta":d,"finish_reason":f}]})+"\n\n").encode()
            self.wfile.write(ev2({"role":"assistant","content":""}))
            self.wfile.write(ev2({"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"terminal","arguments":json.dumps({"command":"sleep 8; echo toolstep-done"})}}]}))
            self.wfile.write(ev2({},"tool_calls")); self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush(); self.close_connection=True; return
        text = ("Short answer to: "+(" ".join(ids) or last.strip()[-40:])) if "SHORT" in last else (LONG[:4000] if "MEDIUM" in last else LONG)
        sys.stderr.write(f"req stream={req.get('stream')} last={last[-80:]!r} len={len(text)}\n"); sys.stderr.flush()
        if not req.get("stream"):
            return self.j(200,{"id":"x","object":"chat.completion","model":"stub","choices":[{"index":0,"message":{"role":"assistant","content":text},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}})
        self.send_response(200); self.send_header("Content-Type","text/event-stream"); self.send_header("Cache-Control","no-cache"); self.send_header("Connection","close"); self.end_headers()
        def ev(d,f=None): return ("data: "+json.dumps({"id":"x","object":"chat.completion.chunk","model":"stub","choices":[{"index":0,"delta":d,"finish_reason":f}]})+"\n\n").encode()
        try:
            self.wfile.write(ev({"role":"assistant","content":""})); self.wfile.flush()
            for i in range(0,len(text),TOK):
                self.wfile.write(ev({"content":text[i:i+TOK]})); self.wfile.flush(); time.sleep(1/TPS)
            self.wfile.write(ev({},"stop")); self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush()
        except (BrokenPipeError,ConnectionResetError): pass
        self.close_connection=True
ThreadingHTTPServer(("127.0.0.1",int(sys.argv[1])),H).serve_forever()
