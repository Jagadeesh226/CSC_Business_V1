from app.main import app


def test_schema_names_do_not_collide():
    """Two Pydantic models with the same class name make FastAPI rename both
    (e.g. 'app__schemas__fee__FeeType'), silently renaming an existing type in
    the API contract. Every schema must keep its plain class name."""
    names = app.openapi()["components"]["schemas"]
    assert [n for n in names if n.startswith("app__")] == []
