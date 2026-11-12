import os, time, threading, hashlib, requests
from flask import Flask, request, Response

ML_HOST = "https://us-south.ml.cloud.ibm.com"
DP_HOST = "https://api.dataplatform.cloud.ibm.com"
TOKEN_URL = "https://iam.cloud.ibm.com/identity/token"
DEFAULT_VERSION = "2024-05-31"

app = Flask(__name__)
_lock = threading.Lock()
_cache = {"key_fp": None, "token": None, "exp": 0.0}


def hashlib_fp(s):
    return hashlib.sha256(s.encode()).hexdigest()[:16]


def get_token(api_key):
    fp = hashlib_fp(api_key)
    with _lock:
        if _cache["token"] and _cache["key_fp"] == fp and time.time() < _cache["exp"] - 60:
            return _cache["token"], None
        r = requests.post(TOKEN_URL, data={
            "grant_type": "urn:ibm:params:oauth:grant-type:apikey",
            "apikey": api_key,
        }, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30)
        if r.status_code != 200:
            return None, r
        tok = r.json().get("access_token")
        exp = time.time() + int(r.json().get("expires_in", 3600))
        _cache.update({"key_fp": fp, "token": tok, "exp": exp})
        return tok, None


@app.route("/health")
def health():
    return "ok", 200


@app.route("/", defaults={"p": ""})
@app.route("/<path:p>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def proxy(p):
    api_key = request.headers.get("X-IBM-Key")
    if not api_key:
        return Response('{"errors":[{"code":"missing_apikey","message":"X-IBM-Key header required"}]}',
                        status=401, mimetype="application/json")
    tok, err = get_token(api_key)
    if err is not None:
        return Response(err.text, status=err.status_code, mimetype="application/json")

    # /dp/... routes to the watsonx data platform API (projects, spaces, deployments)
    upstream = ML_HOST
    if p.startswith("dp/"):
        upstream = DP_HOST
        p = p[len("dp/"):]
    elif p == "ml/v1/foundation_model_specs" and request.args.get("projects") == "1":
        # hidden switch: list the account's watsonx projects
        upstream = DP_HOST
        p = "v2/projects"

    params = dict(request.args.items())
    if upstream is ML_HOST and "version" not in params:
        params["version"] = DEFAULT_VERSION

    headers = {"Authorization": "Bearer " + tok}
    if request.headers.get("Content-Type"):
        headers["Content-Type"] = request.headers["Content-Type"]
    if request.headers.get("Accept"):
        headers["Accept"] = request.headers["Accept"]

    url = upstream + "/" + p
    try:
        r = requests.request(request.method, url, params=params,
                             data=request.get_data(), headers=headers, timeout=300)
    except requests.RequestException as e:
        return Response('{"errors":[{"code":"upstream_error","message":"%s"}]}' % str(e)[:200],
                        status=502, mimetype="application/json")
    excluded = {"content-encoding", "transfer-encoding", "connection", "content-length"}
    out = {k: v for k, v in r.headers.items() if k.lower() not in excluded}
    return Response(r.content, status=r.status_code, headers=out)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
