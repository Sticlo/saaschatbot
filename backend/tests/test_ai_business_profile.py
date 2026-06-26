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


def test_business_summary_lines():
    lines = business_summary_lines(
        business_name="Mi Tienda",
        answers={"tone": "Cercano"},
    )
    assert any("Cercano" in line for line in lines)
