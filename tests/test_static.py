import os
import pytest
from httpx import AsyncClient, ASGITransport
from jsonee import JsonEE


pytestmark = pytest.mark.asyncio


@pytest.fixture
def static_dir(tmp_path):
    (tmp_path / "index.html").write_text("<!doctype html><html><body>hi</body></html>")
    (tmp_path / "app.js").write_text("console.log('x');")
    sub = tmp_path / "css"
    sub.mkdir()
    (sub / "main.css").write_text("body{color:red}")
    return str(tmp_path)


@pytest.fixture
def app(static_dir):
    app = JsonEE()
    app.static("/", static_dir)

    @app.route("GET", "/api/v3/ping")
    def _(req):
        return {"ok": True}

    return app


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_static_serves_index_at_root(client):
    r = await client.get("/")
    assert r.status_code == 200
    assert b"<!doctype html>" in r.content
    assert r.headers["content-type"].startswith("text/html")


async def test_static_serves_named_file(client):
    r = await client.get("/app.js")
    assert r.status_code == 200
    assert r.text == "console.log('x');"
    assert "javascript" in r.headers["content-type"]


async def test_static_serves_subdir(client):
    r = await client.get("/css/main.css")
    assert r.status_code == 200
    assert r.text == "body{color:red}"
    assert "css" in r.headers["content-type"]


async def test_static_path_traversal_blocked(client):
    r = await client.get("/../../etc/passwd")
    assert r.status_code == 404


async def test_api_route_still_reachable_alongside_static(client):
    r = await client.get("/api/v3/ping")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


async def test_static_missing_file_404(client):
    r = await client.get("/nope.js")
    assert r.status_code == 404
