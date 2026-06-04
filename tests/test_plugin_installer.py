import asyncio
import types

from app.services.plugin_installer import PluginInstaller


class FakeCollection:
    def __init__(self):
        self.calls = []

    async def update_one(self, filter_doc, update_doc, upsert=False):
        # Replica il vincolo Mongo: $set su _id immutabile -> errore.
        if "_id" in update_doc.get("$set", {}):
            raise AssertionError(
                "Performing an update on the path '_id' would modify the "
                "immutable field '_id'"
            )
        self.calls.append(
            {"filter": filter_doc, "update": update_doc, "upsert": upsert}
        )


class FakeDb:
    def __init__(self, coll):
        self.engine = types.SimpleNamespace(get_collection=lambda name: coll)


def test_upsert_all_strips_id_from_set():
    coll = FakeCollection()
    db = FakeDb(coll)
    installer = PluginInstaller(cfg={})

    records = [
        {"_id": "6a1fe4fb1ec8e6f72599fabf", "rec_name": "a1", "title": "A1"},
        {"rec_name": "a2", "title": "A2"},
        {"title": "no_rec_name"},  # scartato
    ]

    count = asyncio.run(installer._upsert_all("component", records, db))

    assert count == 2
    assert [c["filter"] for c in coll.calls] == [
        {"rec_name": "a1"},
        {"rec_name": "a2"},
    ]
    # _id MAI nel $set (invariante che evita il code 66 a ogni boot)
    for call in coll.calls:
        assert "_id" not in call["update"]["$set"]
    # i restanti campi restano
    assert coll.calls[0]["update"]["$set"] == {"rec_name": "a1", "title": "A1"}
    assert all(c["upsert"] for c in coll.calls)


def test_upsert_all_record_with_id_does_not_raise():
    coll = FakeCollection()
    db = FakeDb(coll)
    installer = PluginInstaller(cfg={})

    # Senza lo strip, la FakeCollection solleverebbe (come Mongo code 66).
    asyncio.run(
        installer._upsert_all(
            "action",
            [{"_id": "deadbeefdeadbeefdeadbeef", "rec_name": "x"}],
            db,
        )
    )

    assert len(coll.calls) == 1
    assert coll.calls[0]["update"] == {"$set": {"rec_name": "x"}}
