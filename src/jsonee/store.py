"""Storage adapters.

MongoStore — thin pymongo wrapper that speaks the MongoDB wire protocol.
    Works with secantusdb (embedded), real MongoDB, and FerretDB. This is
    the only store: secantusdb stands in for FerretDB/MongoDB, so the same
    adapter serves tests (``:memory:``), local runs (a storage dir), and
    production (a real ``NYRKIO_MONGO_URI``).

open_store(storage_path, uri) — factory used by build_app() when no store
    is supplied:
      • NYRKIO_MONGO_URI set → MongoStore pointed at that URI
      • Otherwise → embedded secantusdb at storage_path
        (defaults to ":memory:" — ephemeral, good for tests and
        short-lived processes; pass a real directory for persistence)
"""
import os

from purejson import Document


class Store:
    """Abstract interface. Duck-typed, not statically typed."""

    def collection(self, name):
        raise NotImplementedError


# ---------------------------------------------------------------------------
# MongoStore — pymongo-backed, works with secantusdb / MongoDB / FerretDB
# ---------------------------------------------------------------------------

class _MongoCollection:
    """Adapts a pymongo Collection to the Store collection interface."""

    def __init__(self, coll):
        self._coll = coll

    def insert_one(self, doc):
        raw = dict(doc.data) if hasattr(doc, "data") else dict(doc)
        result = self._coll.insert_one(raw)
        return result.inserted_id

    def insert_many(self, docs):
        return [self.insert_one(d) for d in docs]

    def find_one(self, filter_=None):
        raw = self._coll.find_one(filter_ or {})
        return Document(raw) if raw is not None else None

    def find(self, filter_=None, sort=None, limit=None):
        kwargs = {}
        if sort:
            # Accept dict {"field": 1/-1} (purejson convention) or list of tuples.
            if isinstance(sort, dict):
                kwargs["sort"] = list(sort.items())
            else:
                kwargs["sort"] = sort
        if limit:
            kwargs["limit"] = limit
        return [Document(d) for d in self._coll.find(filter_ or {}, **kwargs)]

    def count(self, filter_=None):
        return self._coll.count_documents(filter_ or {})


class MongoStore(Store):
    """Store backed by any MongoDB-wire-protocol server (secantusdb, MongoDB, FerretDB).

    ``uri``      — standard MongoDB connection URI.
    ``db_name``  — database name (default "nyrkio"; override via NYRKIO_MONGO_DB).
    ``_server``  — optional SecantusDBServer instance to keep alive and stop().
    """

    def __init__(self, uri, db_name=None, _server=None):
        from pymongo import MongoClient
        self._server = _server
        # tz_aware=True: MongoDB stores dates as UTC and drops the tzinfo;
        # decode them back as timezone-aware UTC so the rest of the stack
        # (extjson refuses naive datetimes) never sees a naive value. The
        # backend (secantusdb / MongoDB / FerretDB) is irrelevant — this is
        # a client-side BSON decoding option.
        self._client = MongoClient(uri, tz_aware=True)
        self._db = self._client[db_name or os.environ.get("NYRKIO_MONGO_DB", "nyrkio")]

    def collection(self, name):
        return _MongoCollection(self._db[name])

    def stop(self):
        self._client.close()
        if self._server is not None:
            self._server.stop()


def open_store(storage_path=None, uri=None):
    """Factory used by build_app() when no explicit store is supplied.

    Resolution order:
    1. ``uri`` argument
    2. ``NYRKIO_MONGO_URI`` environment variable
    3. Embedded secantusdb at ``storage_path``
       (``":memory:"`` when storage_path is None — ephemeral, good for tests)
    """
    uri = uri or os.environ.get("NYRKIO_MONGO_URI")
    if uri:
        return MongoStore(uri)

    from secantus import SecantusDBServer
    path = storage_path or ":memory:"
    if path != ":memory:":
        os.makedirs(path, exist_ok=True)
    server = SecantusDBServer(port=0, storage_path=path)
    server.start()
    return MongoStore(server.uri, _server=server)
