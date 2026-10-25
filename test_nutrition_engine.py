"""Tests for the rule layer. Run: python -m pytest -q

Reference figures come from the ICMR-NIN 2020 Short Report (Tables 1a, 2a and
the RDA/EAR summaries) and the ICMR-NIN Dietary Guidelines for Indians 2024.
"""
import pytest

from nutrition_engine import (
    DietPlanGenerator, FoodPlanner, MaternalProfile, NutritionCalculator,
)

BASE = dict(age=27, weight_kg=60, height_cm=160, activity_level="Moderate", diet_type="Vegetarian")
REF_WOMAN = dict(age=25, weight_kg=55, height_cm=158, diet_type="Vegetarian")  # NIN-2020 reference


def compute(**kw):
    return NutritionCalculator.compute(MaternalProfile(**kw))


@pytest.mark.parametrize("activity,icmr_kcal", [("Sedentary", 1660), ("Moderate", 2130), ("Heavy", 2720)])
def test_energy_reproduces_icmr_reference_woman(activity, icmr_kcal):
    t = compute(**REF_WOMAN, activity_level=activity, trimester="1st")
    assert abs(t.calorie_target - icmr_kcal) <= 0.01 * icmr_kcal


def test_first_trimester_has_no_addition():
    t = compute(trimester="1st", **BASE)
    assert t.pregnancy_kcal_addition == 0
    assert t.protein_target_g == round(0.83 * 60, 1)


def test_second_and_third_trimester_additions():
    t2 = compute(trimester="2nd", **BASE)
    t3 = compute(trimester="3rd", **BASE)
    assert t2.pregnancy_kcal_addition == 350 and t3.pregnancy_kcal_addition == 350
    assert t2.pregnancy_protein_addition_g == 9.5
    assert t3.pregnancy_protein_addition_g == 22.0
    assert round(t3.protein_target_g - t2.protein_target_g, 1) == 12.5
    assert t3.protein_ear_g == round(0.66 * 60 + 17.6, 1)


def test_week_overrides_trimester():
    p = MaternalProfile(gestational_week=14, trimester="1st", **BASE)
    assert p.effective_trimester == "2nd"
    assert MaternalProfile(gestational_week=28, **BASE).effective_trimester == "3rd"


def test_underweight_gets_extra_100_kcal_from_second_trimester():
    thin = dict(BASE, weight_kg=42)  # BMI 16.4
    assert compute(trimester="2nd", **thin).pregnancy_kcal_addition == 450
    assert compute(trimester="1st", **thin).pregnancy_kcal_addition == 0


def test_lactation_additions():
    t = compute(stage="Breastfeeding 0-6 months", **BASE)
    assert t.pregnancy_kcal_addition == 600 and t.pregnancy_protein_addition_g == 17.0
    assert t.micronutrients["Calcium"] == "1200 mg"
    assert t.weight_gain_range_kg is None


def test_pregnancy_micronutrients_match_icmr_rda():
    t = compute(trimester="2nd", **BASE)
    assert t.micronutrients["Iron"] == "27 mg"
    assert t.micronutrients["Folate"] == "570 µg"
    assert t.micronutrients["Iodine"] == "220 µg"
    assert t.micronutrients["Niacin (B3)"] == "16 mg"  # 14 (moderate woman) + 2


def test_adolescent_takes_higher_of_age_and_pregnancy_values():
    t = compute(trimester="2nd", **dict(BASE, age=17))
    assert t.micronutrients["Iron"] == "32 mg"      # girls 16-18 > pregnancy 27
    assert t.micronutrients["Calcium"] == "1050 mg"
    assert t.alerts  # adolescent pregnancy is flagged


def test_weight_gain_range_uses_pre_pregnancy_bmi():
    t = compute(trimester="3rd", current_weight_kg=75, **dict(BASE, weight_kg=62))  # BMI 24.2
    assert t.bmi_category == "Overweight"
    assert t.weight_gain_range_kg == (5.0, 9.0)
    assert t.weight_gain_so_far_kg == 13.0
    assert any("above the recommended" in a for a in t.alerts)


def test_food_basket_matches_dgi_2024_pregnant_chart():
    # DGI-2024 Fig 2.3: sedentary 55 kg woman, ~2020 kcal, 220 g cereals, 75 g pulses, 72 g protein.
    p = MaternalProfile(**REF_WOMAN, activity_level="Sedentary", trimester="3rd")
    fp = FoodPlanner.build(p, NutritionCalculator.compute(p))
    assert abs(fp.kcal - 2020) < 50
    assert abs(fp.groups["Cereals & millets"] - 220) <= 10
    assert abs(fp.groups["Pulses & legumes"] - 75) <= 10
    assert fp.protein_g >= NutritionCalculator.compute(p).protein_target_g


@pytest.mark.parametrize("diet", ["Vegetarian", "Eggetarian", "Non-Vegetarian"])
def test_basket_energy_meets_target_for_every_diet(diet):
    p = MaternalProfile(**dict(BASE, diet_type=diet), trimester="2nd")
    t = NutritionCalculator.compute(p)
    fp = FoodPlanner.build(p, t)
    assert abs(fp.kcal - t.calorie_target) / t.calorie_target < 0.02
    assert fp.protein_g >= t.protein_target_g
    assert ("Egg" in fp.groups) == (diet == "Eggetarian")
    assert ("Fish / chicken" in fp.groups) == (diet == "Non-Vegetarian")
    assert sum(m["kcal"] for m in fp.meals) == pytest.approx(fp.kcal, rel=0.03)


def test_invalid_input_is_rejected():
    with pytest.raises(ValueError):
        compute(trimester="2nd", **{**BASE, "age": 8})
    with pytest.raises(ValueError):
        compute(**BASE)  # pregnant with neither week nor trimester


def test_fallback_plan_is_produced_without_a_key():
    gen = DietPlanGenerator(use_default_client=False)
    plan = gen.generate(MaternalProfile(trimester="3rd", **BASE))
    assert "Day at a glance" in plan.plan_markdown
    assert plan.source == "fallback-template"


def test_total_check_flags_a_drifting_menu():
    t = compute(trimester="2nd", **BASE)
    assert DietPlanGenerator._total_check(f"| **Total** | | **{t.calorie_target:.0f}** | 70 |", t) == ""
    assert "Check" in DietPlanGenerator._total_check("| **Total** | | **1400** | 70 |", t)
