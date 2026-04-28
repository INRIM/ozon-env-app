from app.services.service import _normalize_order


def test_normalize_order_keeps_native_format():
    assert _normalize_order("created_at:desc") == "created_at:desc"
    assert _normalize_order("a:asc,b:desc") == "a:asc,b:desc"


def test_normalize_order_supports_prefix_syntax():
    assert _normalize_order("-created_at") == "created_at:desc"
    assert _normalize_order("+created_at") == "created_at:asc"
    assert _normalize_order("created_at") == "created_at:asc"


def test_normalize_order_supports_multi_field_mixed_syntax():
    assert _normalize_order("-created_at,name,+priority") == (
        "created_at:desc,name:asc,priority:asc"
    )


def test_normalize_order_empty():
    assert _normalize_order("") == ""
