from app import schema


def test_schema_metadata_is_cached_and_can_be_refreshed(monkeypatch):
    inspections = []

    class FakeInspector:
        def __init__(self, revision):
            self.revision = revision

        def get_table_names(self):
            return [f"orders_{self.revision}", "uploaded_datasets"]

        def get_columns(self, table):
            return [{"name": "id", "type": "INTEGER", "nullable": False}]

        def get_pk_constraint(self, table):
            return {"constrained_columns": ["id"]}

        def get_foreign_keys(self, table):
            return []

    def fake_inspect(engine):
        inspector = FakeInspector(len(inspections) + 1)
        inspections.append(inspector)
        return inspector

    monkeypatch.setattr(schema, "inspect", fake_inspect)

    first = schema.get_schema(force_refresh=True)
    cached = schema.get_schema()
    refreshed = schema.get_schema(force_refresh=True)

    assert first == cached
    assert "orders_1:" in first
    assert "uploaded_datasets" not in first
    assert "orders_2:" in refreshed
    assert len(inspections) == 2
