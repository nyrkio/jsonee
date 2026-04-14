"""v0 nyrkiov3 app: two endpoints for ingest + read, in-memory store,
benchzoo-shaped payload. DB-driven: the schemas below are authoritative."""
import datetime
from purejson import Document, Collection
from extjson import ObjectId, dumps
from jsonee import JsonEE, Request, Response, HTTPError, InMemoryStore


SCHEMAS = {
    "Repo": {
        "$id": "Repo",
        "type": "object",
        "properties": {
            "_id": {"type": "objectid"},
            "platform": {"type": "string", "enum": ["gh", "gl"]},
            "namespace": {"type": "string"},
            "repo": {"type": "string"},
            "absolute_name": {"type": "string"},
            "visibility": {"type": "string", "enum": ["public", "private"]},
            "installed_at": {"type": "date"},
        },
        "required": ["platform", "namespace", "repo", "absolute_name"],
    },
    "Metric": {
        "$id": "Metric",
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "unit": {"type": "string"},
            "value": {"type": "number"},
        },
        "required": ["name", "value"],
    },
    "TestRun": {
        "$id": "TestRun",
        "type": "object",
        "properties": {
            "_id": {"type": "objectid"},
            "repo_id": {"type": "objectid"},
            "absolute_name": {"type": "string"},
            "branch": {"type": "string"},
            "git_commit": {"type": "string"},
            "timestamp": {"type": "date"},
            "attributes": {"type": "object"},
            "metrics": {"type": "array", "items": "Metric"},
            "extra_info": {"type": "object"},
            "passed": {"type": "boolean"},
            "source": {"type": "object"},
        },
        "required": ["absolute_name", "timestamp", "attributes", "metrics", "passed"],
    },
    "IngestPayload": {
        "$id": "IngestPayload",
        "type": "object",
        "properties": {
            "runs": {"type": "array", "items": "IngestRun"},
        },
        "required": ["runs"],
    },
    # Ingest format: looks like benchzoo's output — repo fields optional on input,
    # the endpoint fills them from path params.
    "IngestRun": {
        "$id": "IngestRun",
        "type": "object",
        "properties": {
            "branch": {"type": "string"},
            "git_commit": {"type": "string"},
            "timestamp": {"type": ["date", "string", "integer"]},
            "attributes": {"type": "object"},
            "metrics": {"type": "array", "items": "Metric"},
            "extra_info": {"type": "object"},
            "passed": {"type": "boolean"},
        },
        "required": ["attributes", "metrics"],
    },
}


def build_app(store=None):
    store = store or InMemoryStore()
    app = JsonEE(schema_registry=SCHEMAS)
    app.store = store  # attach for tests / handlers

    repos = store.collection("repos")
    runs = store.collection("test_runs")

    def _ensure_repo(platform, namespace, repo):
        absolute = f"{platform}/{namespace}/{repo}"
        existing = repos.find_one({"absolute_name": absolute})
        if existing is not None:
            return existing
        doc = Document(
            platform=platform,
            namespace=namespace,
            repo=repo,
            absolute_name=absolute,
            installed_at=datetime.datetime.now(datetime.timezone.utc),
        )
        repos.insert_one(doc)
        return repos.find_one({"absolute_name": absolute})

    @app.route("POST", "/api/v3/ingest/{platform}/{namespace}/{repo}",
               body_schema="IngestPayload")
    def ingest(request: Request):
        params = request["path_params"]
        platform, namespace, repo = params["platform"], params["namespace"], params["repo"]
        if platform not in ("gh", "gl"):
            raise HTTPError(400, f"unsupported platform {platform!r}")

        repo_doc = _ensure_repo(platform, namespace, repo)
        absolute = repo_doc["absolute_name"]

        payload_runs = request["body"]["runs"]
        inserted = []
        for raw in payload_runs:
            ts = raw.get("timestamp")
            if isinstance(ts, (int, float)):
                ts = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
            elif isinstance(ts, str):
                ts = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            elif ts is None:
                ts = datetime.datetime.now(datetime.timezone.utc)

            doc = Document(
                repo_id=repo_doc["_id"],
                absolute_name=absolute,
                branch=raw.get("branch", "main"),
                git_commit=raw.get("git_commit", ""),
                timestamp=ts,
                attributes=raw["attributes"],
                metrics=raw["metrics"],
                extra_info=raw.get("extra_info", {}),
                passed=raw.get("passed", True),
                source=raw.get("source", {"kind": "api_ingest"}),
            )
            run_id = runs.insert_one(doc)
            inserted.append(run_id)
        return Document(inserted=len(inserted), repo_id=repo_doc["_id"])

    @app.route("GET", "/api/v3/tests/{platform}/{namespace}/{repo}")
    def list_tests(request: Request):
        params = request["path_params"]
        absolute = f"{params['platform']}/{params['namespace']}/{params['repo']}"
        q = request["query"]

        filter_ = {"absolute_name": absolute}
        if "branch" in q:
            filter_["branch"] = q["branch"]
        if "test_name" in q:
            filter_["attributes.test_name"] = q["test_name"]

        def _parse_ts(s):
            dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            return dt

        ts_filter = {}
        if "since" in q:
            ts_filter["$gte"] = _parse_ts(q["since"])
        if "until" in q:
            ts_filter["$lte"] = _parse_ts(q["until"])
        if ts_filter:
            filter_["timestamp"] = ts_filter

        hits = runs.find(filter_, sort={"timestamp": 1})

        # If metric= is specified, narrow each run's metrics list.
        metric_name = q.get("metric")
        if metric_name:
            narrowed = []
            for d in hits:
                sub = [m for m in d.get("metrics", []) if m.get("name") == metric_name]
                if not sub:
                    continue
                nd = Document(dict(d.data))
                nd["metrics"] = sub
                narrowed.append(nd)
            hits = Collection(narrowed)

        return Collection(hits)

    return app
