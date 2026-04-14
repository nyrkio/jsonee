"""ASGI app with route table, middleware event bus, and PureJson request/response.

No dependency injection. Handlers are plain functions taking a Request
and returning a Document / Collection / Response. Middleware hooks into
named events rather than decorating handlers.
"""
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
    """Document with method / path / path_params / query / headers / body."""


class Response(Document):
    """Document with status / headers / body. body is a Document/Collection/None."""

    def __init__(self, body=None, status=200, headers=None):
        super().__init__(
            status=status,
            headers=headers or {"content-type": "application/json"},
            body=body,
        )


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

    # --- registration ---

    def route(self, method, pattern, body_schema=None):
        def decorator(fn):
            self.routes.append(Route(method, pattern, fn, body_schema))
            return fn
        return decorator

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
        query = _parse_query(scope.get("query_string", b""))
        headers = {k.decode(): v.decode() for k, v in scope.get("headers", [])}

        body_bytes = b""
        more = True
        while more:
            msg = await receive()
            body_bytes += msg.get("body", b"")
            more = msg.get("more_body", False)

        body = None
        if body_bytes:
            try:
                body = loads(body_bytes.decode())
            except ValueError as e:
                await _send_error(send, 400, f"invalid JSON: {e}")
                return

        request = Request(
            method=method,
            path=path,
            path_params={},
            query=query,
            headers=headers,
            body=body,
        )

        try:
            self._fire("before_request", request)

            route = None
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

            await _send_response(send, result)
        except HTTPError as he:
            self._fire("on_error", he)
            await _send_error(send, he.status, he.message, he.detail)
        except Exception as e:
            self._fire("on_error", e)
            await _send_error(send, 500, f"internal error: {e}")


def _parse_query(qs_bytes):
    out = {}
    if not qs_bytes:
        return out
    s = qs_bytes.decode() if isinstance(qs_bytes, bytes) else qs_bytes
    for part in s.split("&"):
        if not part:
            continue
        if "=" in part:
            k, v = part.split("=", 1)
        else:
            k, v = part, ""
        from urllib.parse import unquote
        out[unquote(k)] = unquote(v)
    return out


async def _send_response(send, response):
    body_obj = response.get("body")
    body_bytes = dumps(body_obj).encode() if body_obj is not None else b""
    headers = response.get("headers", {})
    await send({
        "type": "http.response.start",
        "status": response.get("status", 200),
        "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
    })
    await send({"type": "http.response.body", "body": body_bytes})


async def _send_error(send, status, message, detail=None):
    body = {"error": message}
    if detail is not None:
        body["detail"] = detail
    body_bytes = dumps(body).encode()
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", b"application/json")],
    })
    await send({"type": "http.response.body", "body": body_bytes})
