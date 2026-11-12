import hashlib, time
import requests
from flask import Flask, request, Response

app = Flask(__name__)
UPSTREAM = "https://us-south.ml.cloud.ibm.com"
TOKEN_URL = "https://iam.cloud.ibm.com/identity/token"
_cache = {}


def bearer_for(apikey):
    h = hashlib.sha256(apikey.encode()).hexdigest()
    hit = _cache.get(h)
    if hit and hit[1] > time.time() + 120:
        return hit[0]
    r = requests.post(TOKEN_URL,
                      data={"grant_type": "urn:ibm:params:oauth:grant-type:apikey", "apikey": apikey},
                      timeout=30)
    if r.status_code != 200:
        try:
            return ("err", r.status_code, r.json().get("errorMessage", "IBM rejected the API key"))
        except Exception:
            return ("err", r.status_code, "IBM rejected the API key")
    j = r.json()
    tok, ttl = j["access_token"], j.get("expires_in", 3600)
    _cache[h] = (tok, time.time() + ttl)
    return tok


@app.get("/health")
def health():
    return "ok", 200


@app.route("/<path:p>", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
def proxy(p):
    key = request.headers.get("X-IBM-Key", "")
    if not key:
        return {"error": "missing X-IBM-Key header"}, 401
    tok = bearer_for(key)
    if isinstance(tok, tuple):
        return {"error": tok[2], "ibm_status": tok[1]}, 401
    headers = {"Authorization": "Bearer " + tok}
    ct = request.headers.get("Content-Type")
    if ct:
        headers["Content-Type"] = ct
    url = f"{UPSTREAM}/{p}"
    if request.query_string:
        url += "?" + request.query_string.decode()
    try:
        r = requests.request(request.method, url, data=request.get_data(), headers=headers, timeout=300)
    except requests.RequestException as e:
        return {"error": "upstream call failed: %s" % e}, 502
    return Response(r.content, status=r.status_code, content_type=r.headers.get("Content-Type", "application/json"))


if __name__ == "__main__":
    import os
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
