from app.shared.core.security import hash_password, validate_password_policy, verify_password


def test_hash_password_roundtrip():
    hashed = hash_password("mi-clave-segura-123")
    assert hashed.startswith("$2")
    assert verify_password("mi-clave-segura-123", hashed)
    assert not verify_password("otra-clave", hashed)


def test_validate_password_policy_rejects_short():
    try:
        validate_password_policy("abc")
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "8" in str(exc)
