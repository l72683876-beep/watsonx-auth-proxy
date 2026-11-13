import os, time, threading, hashlib, json, requests
from flask import Flask, request, Response

ML_HOST = "https://us-south.ml.cloud.ibm.com"
DP_HOST = "https://api.dataplatform.cloud.ibm.com"
TOKEN_URL = "https://iam.cloud.ibm.com/identity/token"
DEFAULT_VERSION = "2024-05-31"

app = Flask(__name__)
_lock = threading.Lock()
_cache = {"key_fp": None, "token": None, "exp": 0.0}


def _fp(s):
    return hashlib.sha256(s.encode()).hexdigest()[:16]


def get_token(api_key):
    fp = _fp(api_key)
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

    params = dict(request.args.items())
    upstream = ML_HOST

    # /dp/... routes to the watsonx data platform API (projects, spaces, deployments)
    if p.startswith("dp/"):
        upstream = DP_HOST
        p = p[len("dp/"):]
    elif p == "ml/v1/foundation_model_specs":
        # hidden switches on the model-specs route
        if params.pop("projects", None) == "1":
            upstream = DP_HOST
            p = "v2/projects"
        elif params.pop("tunings", None) == "1":
            p = "ml/v1/tunings"
        elif params.pop("trainings", None) == "1":
            p = "ml/v4/trainings"
        elif params.pop("tunable", None) == "1":
            params["filters"] = "function.prompt_tuning"
        elif params.pop("finetunable", None) == "1":
            params["filters"] = "function.fine_tuning"
        else:
            fwd = params.pop("forward", None)
            if fwd and (fwd.startswith("ml/") or fwd.startswith("v2/") or fwd.startswith("v4/")):
                p = fwd
                if fwd.startswith("v2/"):
                    upstream = DP_HOST

    # fetch-from-url then PUT-as-file: loads JSONL from GitHub raw into a data asset
    # attachment without streaming bytes through the caller
    fetch_put = params.pop("fetch_put", None)
    fetch_to = params.pop("fetch_to", None)
    if fetch_put and fetch_to:
        try:
            src = requests.get(fetch_put, timeout=120)
            up = fetch_to if fetch_to.startswith("http") else DP_HOST + fetch_to
            h = {"Authorization": "Bearer " + tok, "Content-Type": "application/octet-stream"}
            q = {k: v for k, v in params.items() if k in ("project_id",)}
            r = requests.put(up, data=src.content, headers=h, params=q, timeout=300)
        except requests.RequestException as e:
            return Response('{"errors":[{"code":"fetch_put_error","message":"%s"}]}' % str(e)[:200],
                            status=502, mimetype="application/json")
        excluded = {"content-encoding", "transfer-encoding", "connection", "content-length"}
        out = {k: v for k, v in r.headers.items() if k.lower() not in excluded}
        return Response(r.content, status=r.status_code, headers=out)

    # POST triggered via GET with the JSON body in a query param: some clients cannot
    # send raw POST through this relay, so the proxy issues the POST itself
    post_body = params.pop("post_body", None)
    method = request.method
    data = request.get_data()
    ctype = request.headers.get("Content-Type")
    if post_body:
        method = "POST"
        data = post_body.encode()
        ctype = "application/json"

    if upstream is ML_HOST and "version" not in params:
        params["version"] = DEFAULT_VERSION

    headers = {"Authorization": "Bearer " + tok}
    if ctype:
        headers["Content-Type"] = ctype
    if request.headers.get("Accept"):
        headers["Accept"] = request.headers["Accept"]

    url = upstream + "/" + p
    try:
        r = requests.request(method, url, params=params,
                             data=data, headers=headers, timeout=300)
    except requests.RequestException as e:
        return Response('{"errors":[{"code":"upstream_error","message":"%s"}]}' % str(e)[:200],
                        status=502, mimetype="application/json")
    excluded = {"content-encoding", "transfer-encoding", "connection", "content-length"}
    out = {k: v for k, v in r.headers.items() if k.lower() not in excluded}
    return Response(r.content, status=r.status_code, headers=out)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
