"""Storage adapter. v0 ships an in-memory implementation with a
FerretDB/MongoDB-shaped interface so swapping to the real DB later is
mechanical. Query logic lives in purejson — this module is just a container
that names a collection and hands out query access."""
from purejson import Document, Collection
from extjson import ObjectId


class Store:
    """Abstract interface. Duck-typed, not statically typed."""

    def collection(self, name):
        raise NotImplementedError


class _Collection:
    def __init__(self):
        self._docs = Collection()

    def insert_one(self, doc):
        raw = dict(doc.data) if isinstance(doc, Document) else dict(doc)
        if "_id" not in raw:
            raw["_id"] = ObjectId()
        self._docs.append(raw)
        return raw["_id"]

    def insert_many(self, docs):
        return [self.insert_one(d) for d in docs]

    def find_one(self, filter_=None):
        return self._docs.find_one(filter_ or {})

    def find(self, filter_=None, sort=None, limit=None):
        return self._docs.query(filter_ or {}, sort=sort, limit=limit)

    def count(self, filter_=None):
        if not filter_:
            return len(self._docs.data)
        return len(self._docs.query(filter_).data)


class InMemoryStore(Store):
    def __init__(self):
        self._collections = {}

    def collection(self, name):
        if name not in self._collections:
            self._collections[name] = _Collection()
        return self._collections[name]
