from app.application.ai.ai_business_profile_service import (
    build_ai_system_prompt_from_answers,
    business_summary_lines,
)


def test_build_prompt_from_answers():
    prompt = build_ai_system_prompt_from_answers(
        business_name="Mi Tienda",
        answers={
            "industry": "Restaurante",
            "products_services": "Almuerzos ejecutivos",
            "price_range": "Desde $18.000",
        },
    )
    assert "Mi Tienda" in prompt
    assert "Restaurante" in prompt
    assert "Almuerzos ejecutivos" in prompt
    assert "(tú)" in prompt


def test_build_prompt_uses_usted_when_tone_asks_for_it():
    prompt = build_ai_system_prompt_from_answers(
        business_name="Hotel", answers={"tone": "Formal y amable, de usted"}
    )
    assert "de usted, nunca de tú" in prompt
    assert "(tú)" not in prompt


def test_business_summary_lines():
    lines = business_summary_lines(
        business_name="Mi Tienda",
        answers={"tone": "Cercano"},
    )
    assert any("Cercano" in line for line in lines)
