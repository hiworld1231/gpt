#!/usr/bin/env python3
import json, os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import requests

PORT = int(os.getenv('LLM_ROUTER_PORT', '8099'))
PROVIDERS = {
  'groq': ('https://api.groq.com/openai/v1', 'GROQ_API_KEY'),
  'cerebras': ('https://api.cerebras.ai/v1', 'CEREBRAS_API_KEY'),
  'mistral': ('https://api.mistral.ai/v1', 'MISTRAL_API_KEY'),
  'openrouter': ('https://openrouter.ai/api/v1', 'OPENROUTER_API_KEY'),
}

def route(model, body):
    provider, name = model.split(':', 1) if ':' in model else ('groq', model)
    base, keyname = PROVIDERS.get(provider, PROVIDERS['groq'])
    key = os.getenv(keyname, '')
    if not key: raise RuntimeError(f'{provider} key missing')
    payload = dict(body); payload['model'] = name
    r = requests.post(base + '/chat/completions', headers={'Authorization': 'Bearer '+key, 'Content-Type':'application/json'}, json=payload, timeout=120)
    return r

class H(BaseHTTPRequestHandler):
    def sendj(self, code, obj):
        raw=json.dumps(obj,ensure_ascii=False).encode(); self.send_response(code); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def log_message(self,*a): print('router:', *a, flush=True)
    def do_GET(self): self.sendj(200, {'ok':True})
    def do_POST(self):
        if self.path not in ('/v1/chat/completions','/chat/completions'): return self.sendj(404, {'error':{'message':'not found'}})
        try:
            n=int(self.headers.get('Content-Length','0')); body=json.loads(self.rfile.read(n)); r=route(str(body.get('model','')), body)
            try: obj=r.json()
            except: obj={'error':{'message':r.text[:1000]}}
            self.sendj(r.status_code,obj)
        except Exception as e: self.sendj(500, {'error':{'message':str(e)}})

if __name__ == '__main__': ThreadingHTTPServer(('127.0.0.1', PORT), H).serve_forever()
