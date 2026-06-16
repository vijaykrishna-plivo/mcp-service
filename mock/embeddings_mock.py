"""OpenAI-compatible /v1/embeddings mock (stdlib only).

ACI uses OpenAI embeddings for app/function *semantic search* and at app-upsert
time. Direct function execution by name never touches embeddings, so for the
pilot a deterministic fake is enough: same input text always yields the same
vector, so upserts are stable. Search ranking will be meaningless — swap in a
real OPENAI_API_KEY (and remove OPENAI_BASE_URL) if you want to evaluate
ACI_SEARCH_FUNCTIONS quality.
"""

import hashlib
import json
import random
from http.server import BaseHTTPRequestHandler, HTTPServer

DIM = 1024  # must match SERVER_OPENAI_EMBEDDING_DIMENSION


def embed(text: str) -> list[float]:
	seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big")
	rng = random.Random(seed)
	vec = [rng.uniform(-1, 1) for _ in range(DIM)]
	norm = sum(v * v for v in vec) ** 0.5
	return [v / norm for v in vec]


class Handler(BaseHTTPRequestHandler):
	def do_POST(self):  # noqa: N802
		body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
		inputs = body.get("input", "")
		if isinstance(inputs, str):
			inputs = [inputs]
		data = [
			{"object": "embedding", "index": i, "embedding": embed(t)}
			for i, t in enumerate(inputs)
		]
		resp = json.dumps(
			{
				"object": "list",
				"data": data,
				"model": body.get("model", "text-embedding-3-small"),
				"usage": {"prompt_tokens": 0, "total_tokens": 0},
			}
		).encode()
		self.send_response(200)
		self.send_header("Content-Type", "application/json")
		self.send_header("Content-Length", str(len(resp)))
		self.end_headers()
		self.wfile.write(resp)

	def log_message(self, *args):  # silence per-request noise
		pass


if __name__ == "__main__":
	print(f"mock embeddings listening on :9100 (dim={DIM})", flush=True)
	HTTPServer(("0.0.0.0", 9100), Handler).serve_forever()
