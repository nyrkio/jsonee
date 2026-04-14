# JsonEE — PLAN

End-to-end JSON web framework. PureJson objects everywhere: HTTP body → middleware → DocumentDB and back, no transformation layer.

## Why it exists
Master plan: FastAPI + fastapi_users + pydantic forced bad API shapes ("the auth mechanism demands this"). JsonEE inverts that — the data shape is the contract, the framework adapts.

## Core principles
- **No dependency injection.** Handlers are plain functions taking a request Document, returning a response Document/Collection.
- **Middleware on named events**, not decorators-on-handlers. Events: `before_request`, `before_authentication`, `after_successful_authentication`, `before_authorization`, `before_handler`, `after_handler`, `before_response`, `on_error`.
- **No python typing.** Schemas (ExtendedJsonSchema) are the type system.
- **Persistence is a middleware concern**, not a separate ORM layer. A Document loaded from DocumentDB is the same Document the handler sees and the same Document serialized to the response.

<<< Add: Progressive definition of schema. You start with no schema, everything is just  a dict(). Once data structures mature and settle, some of them are "written in stone" .
<<< Test Driven Documentation. If some type or functionality is not covered in docs, it can be fixed by adding a test(s) that cover the missing feature.

## Routing
File-system based or declarative table — TBD. Recommend declarative table (one `routes.py`) for grep-ability.

## Auth
First-class hooks for OAuth2 / OIDC / SAML, but **no library lock-in**. Re-implementing fastapi_users' good bits (token refresh, password reset flows) as plain middleware sequences a user can fork.

## Persistence
Adapter pattern for DocumentDB. Single adapter to start (For clarity: We talk about the Linux Fundation DocumentDB). The adapter exposes Collections that act like in-memory PureJson Collections but lazy-load.

---

## Design decisions (resolved with master)

### Typing
**JSON Schema / MongoDB schema is the only type system.** No python classes, no generated `.pyi` stubs. Devs read the schema. Bootstrap classes (UserDict/UserList/jsonschema validator) are the only allowed exception.

### Runtime introspection — bounded
- Track **shapes**, never raw values. Shapes are represented as **histograms / shapes-with-weights** (e.g., `email` field is string in 98% of observations, missing in 2%; `age` field is int with observed range [13, 102]).
- **Don't instrument every function.** Instrument only the documented surfaces — typically the HTTP API boundary and explicitly-marked library boundaries. Cheap and targeted.
- Per-field redaction (`"x-redact": true`) skips collection entirely; default-deny patterns for keys matching `email|password|token|secret|key|ssn|auth*`.

### "Alternatively use the python class name directly in JSON: `def my_class(parent_class): -> {"$my_class": {...}}`"
This is clever but couples the wire format to python identifier names. Renaming a class becomes a breaking API change. Recommend: schemas declared with `$id`, python classes reference schemas by `$id`, the class name is local to python.

<<<By this I meant, take for example Python's queue. Say that we want to serialize an instance of the queue module/class. In MongoDB extended schema this can be expressed as {'$queue': {'field1':...}}

## Dependencies
- PureJson, ExtendedJsonSchema.
- An ASGI server (uvicorn or hypercorn) — but JsonEE itself is ASGI app, not tied to one server.

<<< We could explicitly require the newest python with threads.

## Open questions
1. WebSockets / SSE for live runner output (master plan mentions "displaying live while a test is running"). Bake into v1 or defer?
2. Streaming JSON responses for large Collection results — needs design.
3. Background jobs / cron — out of scope; recommend a separate adapter (e.g., simple async task runner, or Postgres-LISTEN if the DocumentDB choice gives us that).

<<< Answers: 1. yes why not, good idea. 2. that is IMO overkill but websockets need streaming-like functionality too. 3. Is this hard to do though?
