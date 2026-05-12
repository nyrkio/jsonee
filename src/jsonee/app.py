"""ASGI app with route table, middleware event bus, and PureJson request/response.

No dependency injection. Handlers are plain functions taking a Request
and returning a Document / Collection / Response. Middleware hooks into
named events rather than decorating handlers.
"""
import mimetypes
import os
import re
from purejson import Document, Collection
from extjson import dumps, loads, validate, ValidationError


class HTTPError(Exception):
    def __init__(self, status, message, detail=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.detail = detail


class Request(Document):
    """Document with method / path / path_params / query / headers / body.

    Handlers accumulate human-readable messages via add_message():

        request.add_message("warn", "falling back to cached data")

    These appear in the response envelope's ``_messages`` list.
    """

    def add_message(self, level, text, **extra):
        """Append a message to this request's message list.

        level — one of "debug", "info", "warn", "error", "critical".
        text  — human-readable string shown in the browser.
        extra — any additional fields (e.g. code="PARSE_FAILED").
        """
        msg = {"level": level, "text": text}
        msg.update(extra)
        self.setdefault("_messages", [])
        self["_messages"].append(msg)


class Response(Document):
    """Document with status / headers / body / optional _links.

    body     — the payload (dict, list, or None).
    status   — HTTP status code (default 200).
    headers  — HTTP response headers dict.
    _links   — optional extra links merged into the envelope's _links
               section. Use to add an "html" link or other relations:
               Response(body=data, _links={"html": {"href": "/dashboard/..."}})
    """

    def __init__(self, body=None, status=200, headers=None, _links=None):
        super().__init__(
            status=status,
            headers=headers or {"content-type": "application/json"},
            body=body,
        )
        if _links:
            self["_links"] = _links


def _compile_path(pattern):
    param_names = []

    def repl(m):
        param_names.append(m.group(1))
        return r"(?P<" + m.group(1) + r">[^/]+)"

    regex = re.sub(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", repl, pattern)
    return re.compile("^" + regex + "$"), param_names


class Route:
    def __init__(self, method, pattern, handler, body_schema=None):
        self.method = method.upper()
        self.pattern = pattern
        self.regex, self.param_names = _compile_path(pattern)
        self.handler = handler
        self.body_schema = body_schema

    def match(self, method, path):
        if method.upper() != self.method:
            return None
        m = self.regex.match(path)
        if not m:
            return None
        return m.groupdict()


_EVENTS = (
    "before_request",
    "before_authentication",
    "after_successful_authentication",
    "before_authorization",
    "before_handler",
    "after_handler",
    "before_response",
    "on_error",
)


class JsonEE:
    def __init__(self, schema_registry=None):
        self.routes = []
        self.schema_registry = dict(schema_registry or {})
        self.middleware = {e: [] for e in _EVENTS}
        self._static_mounts = []  # list of (url_prefix, abs_directory, default_file)

    # --- registration ---

    def route(self, method, pattern, body_schema=None):
        def decorator(fn):
            self.routes.append(Route(method, pattern, fn, body_schema))
            return fn
        return decorator

    def mount_client(self, url_prefix="/js/lib"):
        """Mount the jsonee.js browser client library at url_prefix.

        The static/ directory sits alongside this package file, so no
        configuration is required — the framework finds its own assets.
        Mounted at the default /js/lib/, the client is reachable at
        /js/lib/jsonee.js from the browser.

        Returns True if the static directory was found and mounted.
        """
        here = os.path.dirname(os.path.abspath(__file__))
        static_dir = os.path.join(here, "static")
        if os.path.isdir(static_dir):
            self.static(url_prefix, static_dir)
            return True
        return False

    def static(self, url_prefix, directory, index="index.html"):
        """Serve files under `directory` at `url_prefix`. Requests matching a
        static mount bypass the JSON route table. Path traversal is blocked."""
        directory = os.path.abspath(directory)
        if not url_prefix.startswith("/"):
            url_prefix = "/" + url_prefix
        if not url_prefix.endswith("/"):
            url_prefix = url_prefix + "/"
        self._static_mounts.append((url_prefix, directory, index))

    def _try_static(self, method, path):
        if method != "GET":
            return None
        for prefix, directory, index in self._static_mounts:
            bare = prefix.rstrip("/")
            if path == bare or path == prefix:
                rel = index
            elif path.startswith(prefix):
                rel = path[len(prefix):]
            else:
                continue
            full = os.path.abspath(os.path.join(directory, rel))
            # Path traversal guard: the resolved path must stay inside directory.
            if not (full == directory or full.startswith(directory + os.sep)):
                continue
            if not os.path.isfile(full):
                continue
            mime, _ = mimetypes.guess_type(full)
            return full, mime or "application/octet-stream"
        return None

    def on(self, event):
        if event not in self.middleware:
            raise ValueError(f"unknown event {event!r}; known: {list(self.middleware)}")

        def decorator(fn):
            self.middleware[event].append(fn)
            return fn
        return decorator

    def _fire(self, event, ctx):
        for fn in self.middleware[event]:
            fn(ctx)

    # --- ASGI ---

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            raise RuntimeError("JsonEE v0 only handles http scope")
        method = scope["method"]
        path = scope["path"]

        # Static mounts short-circuit the JSON pipeline (no middleware, no validation).
        hit = self._try_static(method, path)
        if hit is not None:
            full, mime = hit
            with open(full, "rb") as f:
                data = f.read()
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", mime.encode())],
            })
            await send({"type": "http.response.body", "body": data})
            return

        query = _parse_query(scope.get("query_string", b""))
        headers = {k.decode(): v.decode() for k, v in scope.get("headers", [])}

        body_bytes = b""
        more = True
        while more:
            msg = await receive()
            body_bytes += msg.get("body", b"")
            more = msg.get("more_body", False)

        qs_raw = scope.get("query_string", b"")
        qs_str = qs_raw.decode() if isinstance(qs_raw, bytes) else qs_raw
        self_href = path + ("?" + qs_str if qs_str else "")

        body = None
        if body_bytes:
            try:
                body = loads(body_bytes.decode())
            except ValueError as e:
                await _send_error(send, 400, f"invalid JSON: {e}",
                                  self_href=self_href)
                return

        request = Request(
            method=method,
            path=path,
            path_params={},
            query=query,
            headers=headers,
            body=body,
            _messages=[],
        )

        route = None
        try:
            self._fire("before_request", request)

            for r in self.routes:
                params = r.match(method, path)
                if params is not None:
                    route = r
                    request["path_params"] = params
                    break
            if route is None:
                raise HTTPError(404, f"no route for {method} {path}")

            # Validate body against declared schema, if any.
            if route.body_schema is not None and body is not None:
                try:
                    validate(body, route.body_schema, self.schema_registry)
                except ValidationError as ve:
                    raise HTTPError(400, "body schema validation failed", str(ve))

            self._fire("before_handler", request)
            result = route.handler(request)
            if not isinstance(result, Response):
                result = Response(body=result)
            self._fire("after_handler", result)
            self._fire("before_response", result)

            await _send_response(send, result,
                                 self_href=self_href,
                                 docs_href=f"/jsonapi{route.pattern}",
                                 messages=request.get("_messages") or [])
        except HTTPError as he:
            self._fire("on_error", he)
            await _send_error(send, he.status, he.message, he.detail,
                              self_href=self_href,
                              docs_href=f"/jsonapi{route.pattern}" if route else "",
                              messages=request.get("_messages") or [])
        except Exception as e:
            self._fire("on_error", e)
            await _send_error(send, 500, f"internal error: {e}",
                              self_href=self_href,
                              docs_href=f"/jsonapi{route.pattern}" if route else "",
                              messages=request.get("_messages") or [])


class _QueryDict:
    """Dict-like view over a URL query string that preserves
    multi-valued parameters.

    Scalar access (``q[k]`` / ``q.get(k)``) returns the **first**
    value for ``k`` — matches the single-value handlers we had
    before and keeps existing callers unchanged. ``q.getall(k)``
    returns every value for ``k`` (empty list if the key is absent)
    and is the right accessor for checkbox-style filter dims where
    the browser sends ``?k=a&k=b``. Iteration / ``.items()`` yield
    each distinct key exactly once, in first-occurrence order.
    """
    __slots__ = ("_pairs", "_first")

    def __init__(self, pairs):
        self._pairs = list(pairs)
        self._first: dict = {}
        for k, v in self._pairs:
            self._first.setdefault(k, v)

    def __getitem__(self, k):
        return self._first[k]

    def __contains__(self, k):
        return k in self._first

    def __iter__(self):
        return iter(self._first)

    def __len__(self):
        return len(self._first)

    def __bool__(self):
        return bool(self._first)

    def __repr__(self):
        return f"_QueryDict({self._pairs!r})"

    def get(self, k, default=None):
        return self._first.get(k, default)

    def getall(self, k):
        return [v for (kk, v) in self._pairs if kk == k]

    def items(self):
        return self._first.items()

    def keys(self):
        return self._first.keys()

    def values(self):
        return self._first.values()


def _parse_query(qs_bytes):
    if not qs_bytes:
        return _QueryDict([])
    s = qs_bytes.decode() if isinstance(qs_bytes, bytes) else qs_bytes
    from urllib.parse import unquote_plus
    pairs = []
    for part in s.split("&"):
        if not part:
            continue
        if "=" in part:
            k, v = part.split("=", 1)
        else:
            k, v = part, ""
        pairs.append((unquote_plus(k), unquote_plus(v)))
    return _QueryDict(pairs)


def _build_envelope(data, self_href, docs_href, messages, extra_links=None):
    """Wrap a payload in the standard JsonEE response envelope.

    Every JSON response — success or error — has the same top-level shape:

    {
      "data": <payload or null>,
      "_links": {
        "self": {"href": "..."},
        "docs": {"href": "/jsonapi/..."},   // route → ImplicitOpenApi
        ...                                  // handler-supplied extras
      },
      "_messages": [
        {"level": "info"|"warn"|"error"|..., "text": "..."}
      ]
    }
    """
    links = {}
    if self_href:
        links["self"] = {"href": self_href}
    if docs_href:
        links["docs"] = {"href": docs_href}
    if extra_links:
        links.update(extra_links)
    return {"data": data, "_links": links, "_messages": messages or []}


async def _send_response(send, response, *, self_href="", docs_href="", messages=None):
    body_obj = response.get("body")
    extra_links = response.get("_links") or {}
    envelope = _build_envelope(body_obj, self_href, docs_href, messages, extra_links)
    body_bytes = dumps(envelope).encode()
    headers = response.get("headers", {})
    # A list value emits one header tuple per entry — needed for
    # set-cookie, where multiple cookies on one response is legitimate.
    hdr_tuples = []
    for k, v in headers.items():
        if isinstance(v, (list, tuple)):
            for item in v:
                hdr_tuples.append((k.encode(), str(item).encode()))
        else:
            hdr_tuples.append((k.encode(), str(v).encode()))
    await send({
        "type": "http.response.start",
        "status": response.get("status", 200),
        "headers": hdr_tuples,
    })
    await send({"type": "http.response.body", "body": body_bytes})


async def _send_error(send, status, message, detail=None, *,
                      self_href="", docs_href="", messages=None):
    error_msg = {"level": "error", "text": message}
    if detail is not None:
        error_msg["detail"] = detail
    all_messages = list(messages or []) + [error_msg]
    envelope = _build_envelope(None, self_href, docs_href, all_messages)
    body_bytes = dumps(envelope).encode()
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", b"application/json")],
    })
    await send({"type": "http.response.body", "body": body_bytes})
