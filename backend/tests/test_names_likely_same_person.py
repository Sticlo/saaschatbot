from app.shared.core.phone import names_likely_same_person


def test_exact_name_match_only():
    assert names_likely_same_person("Ana", "ana")


def test_ana_is_not_diana():
    assert not names_likely_same_person("Ana", "Diana")
    assert not names_likely_same_person("Ana", "Diana 🐶🐱")
    assert not names_likely_same_person("Kathe 🧸", "Katherin Roballo")
