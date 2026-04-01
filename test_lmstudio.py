import requests

URL = "http://127.0.0.1:1234/v1/chat/completions"

payload = {
    "model": "openai/gpt-oss-20b",
    "temperature": 0,
    "messages": [
        {"role": "user", "content": "Reply with exactly: server works"}
    ]
}

r = requests.post(URL, json=payload, timeout=120)
print(r.json()["choices"][0]["message"]["content"])
