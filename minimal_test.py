import json
from urllib.request import Request, urlopen

BASE_URL = "http://127.0.0.1:11434"
MODEL = "mixtral:latest"

prompt = """Return exactly this line:
episode_id: E0024
"""

payload = {
    "model": MODEL,
    "prompt": prompt,
    "stream": False,
    "options": {
        "temperature": 0,
        "num_predict": 200,
    }
}

req = Request(
    f"{BASE_URL}/api/generate",
    data=json.dumps(payload).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)

with urlopen(req, timeout=600) as resp:
    raw = resp.read().decode("utf-8", errors="replace")
    print("RAW_HTTP_LEN:", len(raw))
    print("RAW_HTTP_START:\n", raw[:500])
    obj = json.loads(raw)

print("DONE_REASON:", obj.get("done_reason"))
print("EVAL_COUNT:", obj.get("eval_count"))
print("MODEL_RESPONSE:\n", obj.get("response"))
