"""
nutrition_engine.py
-------------------
Domain logic for the maternal diet planner.

Nothing in this file knows about Gradio. It exposes plain Python objects so a
unified healthcare app can do:

    from nutrition_engine import MaternalProfile, DietPlanGenerator

    generator = DietPlanGenerator()          # built once, reused per request
    plan = generator.generate(MaternalProfile(...))
    plan.targets.calorie_target              # numbers for the DB / charts
    plan.food_plan.groups                    # daily food-group quantities
    plan.plan_markdown                       # text for the UI

Design notes
------------
* Deterministic nutrition rules run BEFORE the LLM. The model is a writer, not
  a calculator - it receives the already-computed targets AND a food-group
  basket that already meets them, and only turns that basket into dishes.
* The LLM is behind a small interface (`LLMClient`), so swapping providers is a
  one-class change.
* If no API key is configured the engine still returns a usable plan from a
  deterministic template, so the deployment never shows a stack trace.

Sources (all Government of India unless noted)
----------------------------------------------
[NIN-2020]  ICMR-NIN Expert Group. Nutrient Requirements for Indians: RDA and
            EAR, Short Report, 2020. Energy, protein, micronutrient tables,
            visible fat, fibre, water.
[DGI-2024]  ICMR-NIN. Dietary Guidelines for Indians, 2024. Guideline 2
            (pregnancy and lactation), Table 1.3 (food-group composition),
            Table 1.6 (food baskets), Fig 2.3 (diet chart), Annexure I.
[MoHFW-Ca]  MoHFW. National Guidelines for Calcium Supplementation during
            Pregnancy and Lactation (NHSRC).
[FAO-2004]  FAO/WHO/UNU. Human Energy Requirements, 2004 (BMR equations used
            by NIN-2020, with the Indian correction applied below).
[IOM-2009]  Institute of Medicine. Weight Gain During Pregnancy, 2009 (only
            for the underweight gain range, which DGI-2024 does not give).
"""

from __future__ import annotations

import json
import logging
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# 1. Reference data
# --------------------------------------------------------------------------- #

STAGES = ("Pregnant", "Breastfeeding 0-6 months", "Breastfeeding 7-12 months")
TRIMESTERS = ("1st", "2nd", "3rd")
DIET_TYPES = ("Vegetarian", "Eggetarian", "Non-Vegetarian")

#: Physical Activity Level for Indian women [NIN-2020]. 1.40 is stated in the
#: report; 1.8 and 2.3 reproduce its Table 1a (1660 / 2130 / 2720 kcal at 55 kg).
ACTIVITY_FACTORS: Dict[str, float] = {
    "Sedentary": 1.40,
    "Moderate": 1.80,
    "Heavy": 2.30,
}

#: NIN-2020 lowers FAO/WHO/UNU BMR by 9% for adult Indian women. Children and
#: adolescents keep the unadjusted FAO/WHO/UNU value.
BMR_REDUCTION_ADULT_WOMEN = 0.09
ADULT_AGE = 19  # NIN-2020 adult band starts at 19 y; 16-18 y are adolescent girls

#: Extra energy (kcal/d). Pregnancy: +350 from the 2nd trimester, and +100 more
#: for undernourished women (BMI < 18.5) [DGI-2024 Fig 2.2]. Lactation [NIN-2020].
ENERGY_ADDITION: Dict[str, float] = {
    "1st": 0.0,
    "2nd": 350.0,
    "3rd": 350.0,
    "Breastfeeding 0-6 months": 600.0,
    "Breastfeeding 7-12 months": 520.0,
}
UNDERNOURISHED_EXTRA_KCAL = 100.0

#: Protein additions, g/d [NIN-2020 summary tables]: (RDA, EAR).
PROTEIN_ADDITION: Dict[str, Tuple[float, float]] = {
    "1st": (0.0, 0.0),
    "2nd": (9.5, 7.6),
    "3rd": (22.0, 17.6),
    "Breastfeeding 0-6 months": (17.0, 13.6),
    "Breastfeeding 7-12 months": (13.0, 10.6),
}


def _protein_per_kg(age: int) -> Tuple[float, float]:
    """(RDA, EAR) g/kg/d [NIN-2020]: girls 13-15 y, girls 16-18 y, adults."""
    if age <= 15:
        return 0.87, 0.70
    if age < ADULT_AGE:
        return 0.83, 0.67
    return 0.83, 0.66


#: Body-mass index bands used by DGI-2024 for pregnancy weight-gain advice.
#: (upper bound exclusive, label, recommended total gain kg, source)
BMI_BANDS: List[Tuple[float, str, Tuple[float, float], str]] = [
    (18.5, "Underweight", (12.5, 18.0), "IOM 2009; DGI-2024 asks for closer monitoring"),
    (23.0, "Normal", (10.0, 12.0), "DGI-2024"),
    (27.5, "Overweight", (5.0, 9.0), "DGI-2024"),
    (float("inf"), "Obese", (5.0, 9.0), "DGI-2024"),
]

# Micronutrients [NIN-2020 summary of RDA and EAR]. Values are (RDA, EAR).
# Niacin and B6 depend on activity, so they are stored as additions on top of
# the non-pregnant woman's value and resolved in `_micronutrients`.
MICRO_UNITS: Dict[str, str] = {
    "Iron": "mg", "Folate": "µg", "Calcium": "mg", "Iodine": "µg",
    "Vitamin B12": "µg", "Vitamin A": "µg", "Vitamin D": "IU", "Vitamin C": "mg",
    "Zinc": "mg", "Magnesium": "mg", "Thiamine (B1)": "mg", "Riboflavin (B2)": "mg",
    "Niacin (B3)": "mg", "Vitamin B6": "mg",
}

_STAGE_MICROS: Dict[str, Dict[str, Tuple[float, float]]] = {
    "Pregnant": {
        "Iron": (27, 21), "Folate": (570, 480), "Calcium": (1000, 800), "Iodine": (220, 160),
        "Vitamin B12": (2.45, 2.2), "Vitamin A": (900, 406), "Vitamin D": (600, 400),
        "Vitamin C": (80, 65), "Zinc": (14.5, 12.0), "Magnesium": (440, 370),
        "Thiamine (B1)": (2.0, 1.6), "Riboflavin (B2)": (2.7, 2.3), "Vitamin B6": (2.3, 1.9),
    },
    "Breastfeeding 0-6 months": {
        "Iron": (23, 16), "Folate": (330, 280), "Calcium": (1200, 1000), "Iodine": (280, 200),
        "Vitamin B12": (3.2, 2.8), "Vitamin A": (950, 720), "Vitamin D": (600, 400),
        "Vitamin C": (115, 95), "Zinc": (14.1, 11.8), "Magnesium": (400, 335),
        "Thiamine (B1)": (2.1, 1.7), "Riboflavin (B2)": (3.0, 2.5),
    },
    "Breastfeeding 7-12 months": {
        "Iron": (23, 16), "Folate": (330, 280), "Calcium": (1200, 1000), "Iodine": (280, 200),
        "Vitamin B12": (3.2, 2.8), "Vitamin A": (950, 720), "Vitamin D": (600, 400),
        "Vitamin C": (115, 95), "Zinc": (14.1, 11.8), "Magnesium": (400, 335),
        "Thiamine (B1)": (2.1, 1.7), "Riboflavin (B2)": (2.9, 2.4),
    },
}

#: Niacin addition (RDA, EAR) and B6 addition (RDA, EAR) on the base woman value.
_NIACIN_ADD = {"Pregnant": (2, 2), "Breastfeeding 0-6 months": (5, 4), "Breastfeeding 7-12 months": (5, 4)}
_B6_ADD = {"Breastfeeding 0-6 months": (0.26, 0.22), "Breastfeeding 7-12 months": (0.17, 0.16)}

#: Non-pregnant adult woman, activity dependent [NIN-2020]: (RDA, EAR).
_WOMAN_NIACIN = {"Sedentary": (11, 9), "Moderate": (14, 12), "Heavy": (18, 15)}
_WOMAN_B6 = {"Sedentary": (1.9, 1.6), "Moderate": (1.9, 1.6), "Heavy": (2.4, 2.1)}

#: Adolescent girls [NIN-2020]. A pregnant or breastfeeding adolescent gets the
#: higher of her age-group value and the stage value.
_GIRL_MICROS: Dict[str, Dict[str, Tuple[float, float]]] = {
    "13-15": {
        "Iron": (30, 17), "Folate": (245, 204), "Calcium": (1000, 800), "Iodine": (140, 100),
        "Vitamin B12": (2.2, 2), "Vitamin A": (890, 420), "Vitamin D": (600, 400),
        "Vitamin C": (65, 55), "Zinc": (12.8, 10.7), "Magnesium": (340, 282),
        "Thiamine (B1)": (1.6, 1.3), "Riboflavin (B2)": (2.2, 1.9),
        "Niacin (B3)": (16, 13), "Vitamin B6": (2.2, 1.8),
    },
    "16-18": {
        "Iron": (32, 18), "Folate": (270, 223), "Calcium": (1050, 850), "Iodine": (140, 100),
        "Vitamin B12": (2.2, 2), "Vitamin A": (860, 400), "Vitamin D": (600, 400),
        "Vitamin C": (70, 57), "Zinc": (14.2, 11.8), "Magnesium": (380, 317),
        "Thiamine (B1)": (1.7, 1.4), "Riboflavin (B2)": (2.3, 1.9),
        "Niacin (B3)": (17, 14), "Vitamin B6": (2.3, 1.9),
    },
}

#: Where to find each nutrient in an Indian kitchen [DGI-2024 Guideline 2].
MICRO_SOURCES: Dict[str, str] = {
    "Iron": "GLVs, pulses, dry fruits, eggs, meat; eat with guava, orange or lemon",
    "Folate": "GLVs, pulses, nuts, liver",
    "Calcium": "milk, curd, paneer, ragi, sesame",
    "Iodine": "iodised salt",
    "Vitamin B12": "milk, curd, eggs, fish, meat",
    "Vitamin A": "at least half from animal foods (milk, ghee, eggs); plus carrot, pumpkin, GLVs",
    "Vitamin D": "15 min of direct sunlight daily; eggs, fish",
    "Vitamin C": "guava, amla, orange, lemon, tomato",
    "Zinc": "pulses, nuts, whole grains, meat",
    "Magnesium": "millets, pulses, nuts, GLVs",
    "Thiamine (B1)": "whole grains, millets, pulses",
    "Riboflavin (B2)": "milk, curd, eggs, GLVs",
    "Niacin (B3)": "pulses, groundnuts, whole grains, meat",
    "Vitamin B6": "pulses, whole grains, banana, meat",
}

# --- Food groups ------------------------------------------------------------ #

#: Per 100 g raw edible portion: (protein g, fat g, carbohydrate g, kcal, fibre g)
#: [DGI-2024 Table 1.3]. "Fish / chicken" uses the fish and sea-food row.
FOOD_COMPOSITION: Dict[str, Tuple[float, float, float, float, float]] = {
    "Cereals & millets": (9.3, 1.2, 72.0, 343.0, 6.0),
    "Pulses & legumes": (22.8, 3.0, 49.0, 323.0, 12.0),
    "Green leafy vegetables": (3.8, 0.7, 5.0, 45.0, 2.0),
    "Other vegetables": (1.8, 0.4, 5.0, 35.0, 2.0),
    "Roots & tubers": (1.5, 0.2, 12.0, 59.0, 2.0),
    "Fruits": (1.0, 0.6, 11.0, 59.0, 2.0),
    "Nuts & oilseeds": (17.5, 41.3, 18.0, 516.0, 9.0),
    "Milk & curd": (3.1, 4.2, 5.0, 72.0, 0.0),
    "Egg": (13.3, 10.0, 1.0, 147.0, 0.0),
    "Fish / chicken": (18.4, 3.1, 2.0, 110.0, 0.0),
    "Cooking oil & ghee": (0.0, 100.0, 0.0, 900.0, 0.0),
}
FOOD_UNITS = {"Milk & curd": "ml"}

#: Fixed parts of the daily basket, g or ml raw [DGI-2024 Table 1.6 / Fig 2.3].
#: Cereals and pulses are not fixed: DGI says they scale with body weight and
#: activity, so they are solved for in `FoodPlanner` to close the energy gap.
#: 1st trimester uses the ordinary woman's basket ("nutrient-rich balanced diet").
_BASKET_WOMAN_SEDENTARY = {
    "Green leafy vegetables": 100, "Other vegetables": 200, "Roots & tubers": 100,
    "Fruits": 100, "Nuts & oilseeds": 30, "Milk & curd": 300, "Cooking oil & ghee": 25,
}
_BASKET_WOMAN_ACTIVE = {**_BASKET_WOMAN_SEDENTARY, "Nuts & oilseeds": 40, "Cooking oil & ghee": 30}
_BASKET_PREGNANT = {
    "Green leafy vegetables": 150, "Other vegetables": 200, "Roots & tubers": 100,
    "Fruits": 150, "Nuts & oilseeds": 40, "Milk & curd": 400, "Cooking oil & ghee": 30,
}
_BASKET_LACTATING = {**_BASKET_PREGNANT, "Cooking oil & ghee": 35}

#: Cereal : pulse weight ratio for good protein quality [DGI-2024].
CEREAL_PULSE_RATIO = 3.0
#: DGI-2024 Table 1.6: "30 g of pulses may be substituted with meat or eggs".
PULSE_SUBSTITUTION_G = 30.0
#: DGI-2024 Fig 2.2: marine fish or lean meat 80 g/day. One egg is about 50 g.
ANIMAL_FOOD_G = {"Eggetarian": ("Egg", 50.0), "Non-Vegetarian": ("Fish / chicken", 80.0)}

#: Share of each food group per meal, following the DGI-2024 Fig 2.3 diet chart
#: (fruit moved partly to mid-morning so the day has six small meals, as DGI
#: advises 4-6 meals when nausea is a problem). Each column sums to 1.
MEALS: List[Tuple[str, str]] = [
    ("Early morning", "6 am"),
    ("Breakfast", "8 am"),
    ("Mid-morning", "11 am"),
    ("Lunch", "1 pm"),
    ("Evening snack", "4 pm"),
    ("Dinner", "8 pm"),
]
MEAL_SHARES: Dict[str, Dict[str, float]] = {
    "Early morning": {"Nuts & oilseeds": 0.5, "Milk & curd": 0.125},
    "Breakfast": {"Cereals & millets": 0.27, "Pulses & legumes": 0.27, "Other vegetables": 0.25,
                  "Roots & tubers": 0.25, "Nuts & oilseeds": 0.5, "Cooking oil & ghee": 0.17, "Egg": 1.0},
    "Mid-morning": {"Fruits": 0.5},
    "Lunch": {"Cereals & millets": 0.46, "Pulses & legumes": 0.40, "Green leafy vegetables": 0.5,
              "Other vegetables": 0.375, "Roots & tubers": 0.375, "Cooking oil & ghee": 0.5,
              "Milk & curd": 0.5, "Fish / chicken": 1.0},
    "Evening snack": {"Milk & curd": 0.375, "Fruits": 0.25},
    "Dinner": {"Cereals & millets": 0.27, "Pulses & legumes": 0.33, "Green leafy vegetables": 0.5,
               "Other vegetables": 0.375, "Roots & tubers": 0.375, "Cooking oil & ghee": 0.33,
               "Fruits": 0.25},
}


# --------------------------------------------------------------------------- #
# 2. Data models
# --------------------------------------------------------------------------- #

@dataclass
class MaternalProfile:
    """
    One woman's inputs. Validated, not trusted.

    `weight_kg` is the weight energy and protein are calculated on: the weight
    BEFORE pregnancy (or at the first antenatal visit) for a pregnant woman,
    and the current weight for a breastfeeding mother. NIN-2020 calculates
    pregnancy needs on the pre-pregnancy weight and adds a fixed amount on top,
    so using today's weight would count the pregnancy twice.
    """

    age: int
    weight_kg: float
    height_cm: float
    activity_level: str                       # "Sedentary" | "Moderate" | "Heavy"
    diet_type: str                            # see DIET_TYPES
    stage: str = "Pregnant"                   # see STAGES
    trimester: Optional[str] = None           # "1st" | "2nd" | "3rd"; derived from week if given
    gestational_week: Optional[int] = None    # 1-42
    current_weight_kg: Optional[float] = None # pregnancy only, for weight-gain tracking
    notes: str = ""                           # free text: allergies, cuisine, "no beef", etc.

    @property
    def is_pregnant(self) -> bool:
        return self.stage == "Pregnant"

    @property
    def effective_trimester(self) -> Optional[str]:
        if not self.is_pregnant:
            return None
        if self.gestational_week:
            w = self.gestational_week
            return "1st" if w <= 13 else "2nd" if w <= 27 else "3rd"
        return self.trimester

    @property
    def stage_key(self) -> str:
        """Key into the per-stage tables: a trimester, or the lactation stage."""
        return self.effective_trimester if self.is_pregnant else self.stage

    def validate(self) -> None:
        if not 15 <= self.age <= 50:
            raise ValueError("Age should be between 15 and 50 years.")
        if not 30 <= self.weight_kg <= 150:
            raise ValueError("Weight should be between 30 and 150 kg.")
        if not 130 <= self.height_cm <= 200:
            raise ValueError("Height should be between 130 and 200 cm.")
        if self.activity_level not in ACTIVITY_FACTORS:
            raise ValueError("Activity level should be Sedentary, Moderate or Heavy.")
        if self.stage not in STAGES:
            raise ValueError("Stage should be Pregnant or Breastfeeding.")
        if self.diet_type not in DIET_TYPES:
            raise ValueError("Diet type should be Vegetarian, Eggetarian or Non-Vegetarian.")
        if self.is_pregnant:
            if self.gestational_week is not None and not 1 <= self.gestational_week <= 42:
                raise ValueError("Week of pregnancy should be between 1 and 42.")
            if self.effective_trimester not in TRIMESTERS:
                raise ValueError("Give the week of pregnancy or the trimester (1st, 2nd or 3rd).")
            if self.current_weight_kg is not None:
                gain = self.current_weight_kg - self.weight_kg
                if not -15 <= gain <= 40:
                    raise ValueError("Current weight is very different from the weight before "
                                     "pregnancy. Check both numbers.")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class NutritionTargets:
    """Everything the deterministic layer decides before the LLM is called."""

    bmr_kcal: float
    maintenance_kcal: float
    pregnancy_kcal_addition: float  # stage addition (pregnancy or lactation)
    calorie_target: float

    baseline_protein_g: float
    pregnancy_protein_addition_g: float
    protein_target_g: float         # RDA - the safe intake
    protein_ear_g: float            # EAR - the average requirement

    fat_target_g: float
    carb_target_g: float
    fibre_target_g: float
    water_litres: str

    bmi: float
    bmi_category: str
    weight_gain_range_kg: Optional[Tuple[float, float]] = None
    weight_gain_so_far_kg: Optional[float] = None

    micronutrients: Dict[str, str] = field(default_factory=dict)       # display strings
    micronutrient_table: List[Dict[str, Any]] = field(default_factory=list)
    supplements: List[str] = field(default_factory=list)
    alerts: List[str] = field(default_factory=list)
    calculation_trace: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class FoodPlan:
    """Daily raw quantities per food group, and how they split across meals."""

    groups: Dict[str, float]                       # g (ml for milk) per day
    meals: List[Dict[str, Any]]                    # name, time, items {group: g}, kcal, protein_g
    kcal: float
    protein_g: float
    fat_g: float
    carb_g: float
    fibre_g: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class DietPlan:
    """What the caller gets back."""

    profile: MaternalProfile
    targets: NutritionTargets
    food_plan: FoodPlan
    plan_markdown: str
    source: str  # "llm:<model>" or "fallback-template"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "profile": self.profile.to_dict(),
            "targets": self.targets.to_dict(),
            "food_plan": self.food_plan.to_dict(),
            "plan_markdown": self.plan_markdown,
            "source": self.source,
        }


# --------------------------------------------------------------------------- #
# 3. Deterministic nutrition rules
# --------------------------------------------------------------------------- #

class NutritionCalculator:
    """Pure functions. No I/O, no model calls - easy to unit test."""

    @staticmethod
    def bmr_fao(weight_kg: float, age: int) -> float:
        """FAO/WHO/UNU 2004 female BMR equations, kcal/d."""
        if age < 18:
            return 13.384 * weight_kg + 692.6
        if age < 30:
            return 14.818 * weight_kg + 486.6
        return 8.126 * weight_kg + 845.6

    @classmethod
    def bmr_indian(cls, weight_kg: float, age: int) -> float:
        bmr = cls.bmr_fao(weight_kg, age)
        return bmr * (1 - BMR_REDUCTION_ADULT_WOMEN) if age >= ADULT_AGE else bmr

    @staticmethod
    def bmi_band(bmi: float) -> Tuple[str, Tuple[float, float], str]:
        for upper, label, gain, source in BMI_BANDS:
            if bmi < upper:
                return label, gain, source
        raise AssertionError("unreachable")

    @staticmethod
    def bmi_category(bmi: float) -> str:
        return NutritionCalculator.bmi_band(bmi)[0]

    @staticmethod
    def _micronutrients(profile: MaternalProfile) -> List[Dict[str, Any]]:
        stage = "Pregnant" if profile.is_pregnant else profile.stage
        values = dict(_STAGE_MICROS[stage])

        base_niacin = _WOMAN_NIACIN[profile.activity_level]
        base_b6 = _WOMAN_B6[profile.activity_level]
        girl = None
        if profile.age < ADULT_AGE:
            girl = _GIRL_MICROS["13-15" if profile.age <= 15 else "16-18"]
            base_niacin = girl["Niacin (B3)"]
            base_b6 = girl["Vitamin B6"]

        n_add = _NIACIN_ADD[stage]
        values["Niacin (B3)"] = (base_niacin[0] + n_add[0], base_niacin[1] + n_add[1])
        if "Vitamin B6" not in values:
            b_add = _B6_ADD[stage]
            values["Vitamin B6"] = (round(base_b6[0] + b_add[0], 2), round(base_b6[1] + b_add[1], 2))

        if girl:
            for k, (rda, ear) in girl.items():
                if k in values:
                    values[k] = (max(values[k][0], rda), max(values[k][1], ear))

        return [
            {"nutrient": k, "unit": MICRO_UNITS[k], "rda": values[k][0], "ear": values[k][1],
             "sources": MICRO_SOURCES[k]}
            for k in MICRO_UNITS
        ]

    @staticmethod
    def _supplements(profile: MaternalProfile) -> List[str]:
        """Government programme schedule. Reminders only - doses come from the ANC provider."""
        ifa = ("IFA: one tablet a day (60 mg elemental iron + 500 µg folic acid), "
               "taken about 2 hours after a meal and not with tea, coffee or calcium [DGI-2024].")
        calcium = ("Calcium: one 500 mg tablet (with 250 IU vitamin D3) twice a day, "
                   "kept at least 2 hours apart from IFA [MoHFW-Ca].")
        if profile.is_pregnant:
            week = profile.gestational_week
            tri = profile.effective_trimester
            items = []
            if tri == "1st" and (week is None or week < 12):
                items.append("Folic acid: 500 µg (0.5 mg) a day through the first 12 weeks [DGI-2024].")
            if (week is not None and week >= 12) or tri in ("2nd", "3rd"):
                items.append(ifa + " Started from week 12 and continued until 6 months after delivery.")
            else:
                items.append("From week 12: " + ifa)
            if (week is not None and week >= 14) or tri in ("2nd", "3rd"):
                items.append(calcium + " From week 14 until 6 months after delivery.")
            else:
                items.append("From week 14: " + calcium)
        else:
            items = [ifa + " Continued for the first 6 months after delivery [DGI-2024]."]
            if profile.stage == "Breastfeeding 0-6 months":
                items.append(calcium + " Until 6 months after delivery.")
        items.append("Iodised salt for all cooking, and less than 5 g of salt a day [DGI-2024].")
        return items

    @classmethod
    def compute(cls, profile: MaternalProfile) -> NutritionTargets:
        profile.validate()
        key = profile.stage_key
        trace: List[str] = []

        # --- body size -------------------------------------------------------
        height_m = profile.height_cm / 100
        bmi = profile.weight_kg / (height_m ** 2)
        bmi_label, gain_range, gain_source = cls.bmi_band(bmi)
        weight_word = "pre-pregnancy weight" if profile.is_pregnant else "current weight"
        trace.append(f"BMI on {weight_word} = {profile.weight_kg:.1f} / {height_m:.2f}² = {bmi:.1f} "
                     f"({bmi_label}, DGI-2024 cut-offs 18.5 / 23 / 27.5)")

        # --- energy ----------------------------------------------------------
        bmr_fao = cls.bmr_fao(profile.weight_kg, profile.age)
        bmr = cls.bmr_indian(profile.weight_kg, profile.age)
        pal = ACTIVITY_FACTORS[profile.activity_level]
        maintenance = bmr * pal
        if profile.age >= ADULT_AGE:
            trace.append(f"BMR (FAO/WHO/UNU 2004) = {bmr_fao:.0f} kcal, less 9% for Indian women "
                         f"(NIN-2020) = {bmr:.0f} kcal")
        else:
            trace.append(f"BMR (FAO/WHO/UNU 2004, adolescent - no Indian reduction) = {bmr:.0f} kcal")
        trace.append(f"x activity factor {pal} ({profile.activity_level.lower()}, NIN-2020) "
                     f"= {maintenance:.0f} kcal before pregnancy additions")

        kcal_add = ENERGY_ADDITION[key]
        if profile.is_pregnant and kcal_add > 0 and bmi_label == "Underweight":
            kcal_add += UNDERNOURISHED_EXTRA_KCAL
            trace.append(f"Addition for the {key} trimester = +350 kcal, +100 kcal more because "
                         f"BMI is under 18.5 (DGI-2024)")
        elif profile.is_pregnant:
            trace.append(f"Addition for the {key} trimester = +{kcal_add:.0f} kcal "
                         f"({'none in the 1st trimester' if kcal_add == 0 else 'from the 2nd trimester'}, "
                         f"NIN-2020 / DGI-2024)")
        else:
            trace.append(f"Addition for {key.lower()} = +{kcal_add:.0f} kcal (NIN-2020)")
        calorie_target = maintenance + kcal_add
        trace.append(f"Daily energy target = {calorie_target:.0f} kcal")

        # --- protein ---------------------------------------------------------
        rda_kg, ear_kg = _protein_per_kg(profile.age)
        p_add_rda, p_add_ear = PROTEIN_ADDITION[key]
        baseline_protein = rda_kg * profile.weight_kg
        protein_target = baseline_protein + p_add_rda
        protein_ear = ear_kg * profile.weight_kg + p_add_ear
        trace.append(f"Protein (RDA) = {rda_kg} g/kg x {profile.weight_kg:.1f} kg + {p_add_rda:.1f} g "
                     f"= {protein_target:.1f} g (EAR {protein_ear:.1f} g; NIN-2020)")

        # --- macros, fibre, water -------------------------------------------
        # DGI-2024 balanced diets give ~30% of energy from fat (AMDR 20-35%).
        fat_g = 0.30 * calorie_target / 9
        carb_g = max(0.0, (calorie_target - protein_target * 4 - fat_g * 9) / 4)
        fibre_g = 15.0 * calorie_target / 1000  # 30 g per 2000 kcal, NIN-2020
        water = "2.1-3.2 litres" if profile.is_pregnant else "over 2 litres, plus a glass at each feed"
        trace.append(f"Fat = 30% of energy = {fat_g:.0f} g; carbohydrate = remaining energy = {carb_g:.0f} g "
                     f"(AMDR for pregnancy and lactation: fat 20-35%, carbohydrate 45-65%; NIN-2020)")
        trace.append(f"Fibre = 30 g per 2000 kcal = {fibre_g:.0f} g (NIN-2020)")

        # --- weight gain -----------------------------------------------------
        gain_so_far = None
        gain = None
        if profile.is_pregnant:
            gain = gain_range
            trace.append(f"Recommended total gain for {bmi_label.lower()} BMI = "
                         f"{gain[0]:g}-{gain[1]:g} kg ({gain_source})")
            if profile.current_weight_kg is not None:
                gain_so_far = round(profile.current_weight_kg - profile.weight_kg, 1)

        micro_table = cls._micronutrients(profile)
        alerts = cls._alerts(profile, bmi_label, gain, gain_so_far)

        return NutritionTargets(
            bmr_kcal=round(bmr, 1),
            maintenance_kcal=round(maintenance, 1),
            pregnancy_kcal_addition=kcal_add,
            calorie_target=round(calorie_target, 1),
            baseline_protein_g=round(baseline_protein, 1),
            pregnancy_protein_addition_g=p_add_rda,
            protein_target_g=round(protein_target, 1),
            protein_ear_g=round(protein_ear, 1),
            fat_target_g=round(fat_g, 1),
            carb_target_g=round(carb_g, 1),
            fibre_target_g=round(fibre_g, 1),
            water_litres=water,
            bmi=round(bmi, 1),
            bmi_category=bmi_label,
            weight_gain_range_kg=gain,
            weight_gain_so_far_kg=gain_so_far,
            micronutrients={m["nutrient"]: f"{m['rda']:g} {m['unit']}" for m in micro_table},
            micronutrient_table=micro_table,
            supplements=cls._supplements(profile),
            alerts=alerts,
            calculation_trace=trace,
        )

    @staticmethod
    def _alerts(profile: MaternalProfile, bmi_label: str,
                gain: Optional[Tuple[float, float]], gain_so_far: Optional[float]) -> List[str]:
        alerts = []
        if profile.age < ADULT_AGE:
            alerts.append("Pregnancy under 19 needs extra care: her own body is still growing. "
                          "Needs here use the higher of the adolescent and pregnancy values. "
                          "DGI-2024 advises a first pregnancy at 21 or later.")
        elif profile.age >= 35:
            alerts.append("Age 35 or over: keep every antenatal check-up, including blood pressure "
                          "and blood sugar tests.")
        if bmi_label == "Underweight":
            alerts.append("Underweight before pregnancy: eat more and get weight checked regularly "
                          "at the Anganwadi or health centre. Take-home rations from ICDS help "
                          "(DGI-2024).")
        elif bmi_label in ("Overweight", "Obese"):
            alerts.append(f"{bmi_label} before pregnancy: avoid added sugar and cut down on refined "
                          "cereals and oil, but do not diet or skip meals (DGI-2024). Ask the doctor "
                          "about a blood sugar test.")
        if profile.is_pregnant and profile.activity_level == "Heavy":
            alerts.append("Heavy work: DGI-2024 advises against lifting heavy objects or strenuous "
                          "work during pregnancy. Rest 2-3 hours during the day.")
        if gain and gain_so_far is not None:
            if gain_so_far > gain[1]:
                alerts.append(f"Weight gained so far ({gain_so_far:g} kg) is already above the "
                              f"recommended total of {gain[0]:g}-{gain[1]:g} kg. Discuss with the doctor.")
            elif gain_so_far < 0 and profile.effective_trimester != "1st":
                alerts.append("Weight is below the pre-pregnancy weight after the first trimester. "
                              "Tell the doctor or ANM at the next visit.")
        return alerts


# --------------------------------------------------------------------------- #
# 4. Food-group basket
# --------------------------------------------------------------------------- #

class FoodPlanner:
    """
    Builds the DGI-2024 daily food basket for this woman and resizes the cereal
    and pulse portions (kept at 3:1) so the basket's energy meets the target.
    """

    MIN_PULSES_G = 30.0

    @staticmethod
    def _nutrients(groups: Dict[str, float]) -> Tuple[float, float, float, float, float]:
        p = f = c = e = fi = 0.0
        for name, grams in groups.items():
            cp, cf, cc, ce, cfi = FOOD_COMPOSITION[name]
            k = grams / 100
            p, f, c, e, fi = p + cp * k, f + cf * k, c + cc * k, e + ce * k, fi + cfi * k
        return p, f, c, e, fi

    @classmethod
    def build(cls, profile: MaternalProfile, targets: NutritionTargets) -> FoodPlan:
        key = profile.stage_key
        if key == "1st":
            fixed = dict(_BASKET_WOMAN_SEDENTARY if profile.activity_level == "Sedentary"
                         else _BASKET_WOMAN_ACTIVE)
        elif profile.is_pregnant:
            fixed = dict(_BASKET_PREGNANT)
        else:
            fixed = dict(_BASKET_LACTATING)

        substitution = 0.0
        if profile.diet_type in ANIMAL_FOOD_G:
            food, grams = ANIMAL_FOOD_G[profile.diet_type]
            fixed[food] = grams
            substitution = PULSE_SUBSTITUTION_G

        # Solve for u where cereals = 3u, pulses = u - substitution.
        fixed_kcal = cls._nutrients(fixed)[3]
        cereal_kcal_g = FOOD_COMPOSITION["Cereals & millets"][3] / 100
        pulse_kcal_g = FOOD_COMPOSITION["Pulses & legumes"][3] / 100
        u = (targets.calorie_target - fixed_kcal + substitution * pulse_kcal_g) / (
            CEREAL_PULSE_RATIO * cereal_kcal_g + pulse_kcal_g)
        pulses = max(cls.MIN_PULSES_G, u - substitution)
        u = max(u, pulses + substitution)  # never shrink cereals below the pulse floor's ratio

        groups = {"Cereals & millets": CEREAL_PULSE_RATIO * u, "Pulses & legumes": pulses, **fixed}
        groups = {k: float(round(v / 5) * 5) for k, v in groups.items()}  # round to 5 g

        meals = []
        for meal, time in MEALS:
            items = {g: round(groups[g] * share / 5) * 5
                     for g, share in MEAL_SHARES[meal].items() if g in groups}
            items = {g: q for g, q in items.items() if q > 0}
            p, _, _, e, _ = cls._nutrients(items)
            meals.append({"name": meal, "time": time, "items": items,
                          "kcal": round(e), "protein_g": round(p, 1)})

        p, f, c, e, fi = cls._nutrients(groups)
        return FoodPlan(groups=groups, meals=meals, kcal=round(e), protein_g=round(p, 1),
                        fat_g=round(f, 1), carb_g=round(c, 1), fibre_g=round(fi, 1))


def household_measure(group: str, grams: float) -> str:
    """Rough kitchen equivalents. Spoon and katori sizes are from DGI-2024 Annexure I."""
    if group == "Cooking oil & ghee":
        return f"about {grams / 5:.0f} teaspoons"
    if group == "Milk & curd":
        return f"about {grams / 200:.1f} medium (200 ml) katori or glasses".replace(".0 ", " ")
    if group == "Cereals & millets":
        return f"about {grams / 30:.0f} portions of 30 g (1 portion = 1 medium roti or ½ katori cooked rice)"
    if group == "Pulses & legumes":
        return f"about {grams / 30:.1f} katori cooked dal (30 g raw each)".replace(".0 ", " ")
    if group == "Egg":
        return f"{grams / 50:.0f} egg"
    if group == "Nuts & oilseeds":
        return "a small handful" if grams <= 20 else "two small handfuls"
    return ""


# --------------------------------------------------------------------------- #
# 5. Prompt construction
# --------------------------------------------------------------------------- #

class PromptBuilder:
    """Turns profile + computed targets + food basket into a system/user message pair."""

    SYSTEM = (
        "You are a clinical dietitian who writes daily meal plans for pregnant and breastfeeding "
        "women in India, following the ICMR-NIN Dietary Guidelines for Indians (2024). You write "
        "for the woman herself: short sentences, household measures (katori, glass, roti, spoon), "
        "and foods available in an ordinary Indian kitchen.\n\n"
        "Hard rules:\n"
        "1. The energy and protein targets AND the daily food-group quantities are given to you. "
        "They are already correct. Never recalculate them. Build each meal from the food-group "
        "grams given for that meal, turning them into real dishes.\n"
        "2. Use the kcal and protein figures given for each meal in your table. The Total row "
        "must equal the given daily totals.\n"
        "3. Respect the diet type strictly. Vegetarian: no meat, fish, chicken or egg. "
        "Eggetarian: eggs allowed, no meat, fish or chicken. Respect every allergy and food "
        "avoidance in the patient notes - substitute within the same food group.\n"
        "4. Do not diagnose anything, do not prescribe medicines or supplement doses, and do not "
        "promise outcomes. You may only remind her to take what she has been prescribed.\n"
        "5. Half of cereals should be whole grains, and some should be millets (ragi, jowar, "
        "bajra). Pair iron-rich foods with a vitamin-C fruit; keep tea and coffee away from meals.\n"
        "6. Do not invent food taboos. DGI-2024 says there is no need to avoid foods based on "
        "wrong beliefs. Only list the evidence-based foods to limit.\n"
        "7. Output Markdown only, using the exact section headings requested."
    )

    TEMPLATE = """Create a one-day meal plan.

## Patient details
- Age: {age} years
- Stage: {stage_line}
- {weight_line}
- Height: {height_cm} cm, BMI {bmi} ({bmi_category})
- Activity level: {activity_level}
- Diet type: {diet_type}
- Other notes from the patient: {notes}

## Targets (already calculated - use as given)
- Energy: {calorie_target:.0f} kcal per day
- Protein: at least {protein_target_g:.0f} g per day
- Fat: about {fat_target_g:.0f} g; carbohydrate about {carb_target_g:.0f} g; fibre about {fibre_target_g:.0f} g
- Key micronutrients: {micronutrient_line}

## Daily food-group quantities (raw weight, from the ICMR-NIN 2024 food basket)
{group_lines}

## How the groups split across meals (raw grams, with the kcal and protein each meal provides)
{meal_lines}

## Kitchen measures to use (raw weight -> household amount)
- 1 teaspoon oil or ghee = 5 g; 1 tablespoon = 15 g
- 1 medium katori or 1 glass = 200 ml (milk, curd, buttermilk)
- 30 g atta or millet flour = 1 medium roti; 30 g raw rice = about 1/2 katori cooked rice
- 30 g raw dal = about 1 katori cooked dal
- 20 g nuts = a small handful (NOT a katori); 1 egg = 50 g
- 100 g raw vegetables = about 1 katori cooked sabzi; 100 g fruit = 1 medium fruit

## Required output format
### Day at a glance
One short paragraph, then a Markdown table with columns: Meal | What to eat | Approx. kcal | Approx. protein (g).
Rows: Early morning, Breakfast, Mid-morning, Lunch, Evening snack, Dinner. Give dish names AND household
quantities using only the kitchen measures above.
End the table with a **Total** row.

### Why this plan
Three to five bullets linking specific foods to this stage's needs (iron, folate, calcium, protein, iodine).

### Foods to limit
Four to six bullets (e.g. alcohol and tobacco in any form, excess tea/coffee, unpasteurised milk, raw or undercooked eggs and meat, high-mercury fish, high fat-sugar-salt packaged foods).

### Simple swaps
Three bullets giving a cheaper or easier alternative within the same food group.

### When to call your doctor
Three bullets on warning signs relevant to this stage.
"""

    @classmethod
    def build(cls, profile: MaternalProfile, targets: NutritionTargets,
              food_plan: FoodPlan) -> List[Dict[str, str]]:
        micronutrient_line = "; ".join(f"{k} {v}" for k, v in targets.micronutrients.items())
        if profile.is_pregnant:
            week = f", week {profile.gestational_week}" if profile.gestational_week else ""
            stage_line = f"Pregnant, {profile.effective_trimester} trimester{week}"
            weight_line = f"Weight before pregnancy: {profile.weight_kg} kg"
            if profile.current_weight_kg is not None:
                weight_line += f"; current weight {profile.current_weight_kg} kg"
        else:
            stage_line = profile.stage
            weight_line = f"Current weight: {profile.weight_kg} kg"

        group_lines = "\n".join(f"- {g}: {q:.0f} {FOOD_UNITS.get(g, 'g')}"
                                for g, q in food_plan.groups.items())
        meal_lines = "\n".join(
            f"- {m['name']} ({m['time']}): "
            + ", ".join(f"{g} {q:.0f} {FOOD_UNITS.get(g, 'g')}" for g, q in m["items"].items())
            + f" -> about {m['kcal']} kcal, {m['protein_g']:.0f} g protein"
            for m in food_plan.meals
        )
        meal_lines += f"\n- Daily total: about {food_plan.kcal:.0f} kcal, {food_plan.protein_g:.0f} g protein"

        user = cls.TEMPLATE.format(
            age=profile.age,
            stage_line=stage_line,
            weight_line=weight_line,
            height_cm=profile.height_cm,
            bmi=targets.bmi,
            bmi_category=targets.bmi_category,
            activity_level=profile.activity_level,
            diet_type=profile.diet_type,
            notes=profile.notes.strip() or "none given",
            calorie_target=targets.calorie_target,
            protein_target_g=targets.protein_target_g,
            fat_target_g=targets.fat_target_g,
            carb_target_g=targets.carb_target_g,
            fibre_target_g=targets.fibre_target_g,
            micronutrient_line=micronutrient_line,
            group_lines=group_lines,
            meal_lines=meal_lines,
        )
        return [
            {"role": "system", "content": cls.SYSTEM},
            {"role": "user", "content": user},
        ]


# --------------------------------------------------------------------------- #
# 6. LLM clients
# --------------------------------------------------------------------------- #

class LLMClient(ABC):
    """Swap this out to change provider without touching anything else."""

    @abstractmethod
    def complete(self, messages: List[Dict[str, str]]) -> str: ...

    @property
    @abstractmethod
    def name(self) -> str: ...


class OpenAICompatibleClient(LLMClient):
    """
    Works with any OpenAI-compatible endpoint: OpenAI, Groq, OpenRouter,
    Together, vLLM, Ollama. Only the base URL and model name change.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        temperature: float = 0.4,
        timeout: float = 60.0,
        max_tokens: int = 6000,
        reasoning_effort: Optional[str] = None,
    ):
        from openai import OpenAI  # imported here so the module loads without the SDK

        self._model = model or os.environ.get("LLM_MODEL", "gpt-4o-mini")
        self._temperature = temperature
        # Reasoning models (gpt-oss, o-series) spend completion tokens on hidden
        # reasoning; too small a budget returns an empty answer.
        self._max_tokens = int(os.environ.get("LLM_MAX_TOKENS", max_tokens))
        self._reasoning_effort = reasoning_effort or os.environ.get("LLM_REASONING_EFFORT") or (
            "low" if "gpt-oss" in self._model else None)
        self._client = OpenAI(
            api_key=api_key or os.environ["LLM_API_KEY"],
            base_url=base_url or os.environ.get("LLM_BASE_URL") or None,
            timeout=timeout,
        )

    @property
    def name(self) -> str:
        return f"llm:{self._model}"

    def complete(self, messages: List[Dict[str, str]]) -> str:
        kwargs: Dict[str, Any] = {}
        if self._reasoning_effort:
            kwargs["reasoning_effort"] = self._reasoning_effort
        response = self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            **kwargs,
        )
        choice = response.choices[0]
        if choice.finish_reason == "length":
            logger.warning("Model hit max_tokens (%s); answer may be cut off.", self._max_tokens)
        return (choice.message.content or "").strip()


# --------------------------------------------------------------------------- #
# 7. Orchestration
# --------------------------------------------------------------------------- #

class DietPlanGenerator:
    """
    The class the unified app imports.

        generator = DietPlanGenerator()
        plan = generator.generate(profile)
    """

    #: Allowed gap between the model's Total row and the computed energy target.
    TOTAL_TOLERANCE = 0.10

    def __init__(
        self,
        llm_client: Optional[LLMClient] = None,
        calculator: type[NutritionCalculator] = NutritionCalculator,
        prompt_builder: type[PromptBuilder] = PromptBuilder,
        food_planner: type[FoodPlanner] = FoodPlanner,
        use_default_client: bool = True,
    ):
        self.calculator = calculator
        self.prompt_builder = prompt_builder
        self.food_planner = food_planner
        self.llm_client = llm_client or (self._default_client() if use_default_client else None)

    @staticmethod
    def _default_client() -> Optional[LLMClient]:
        if not os.environ.get("LLM_API_KEY"):
            logger.warning("LLM_API_KEY not set - using the deterministic template plan.")
            return None
        try:
            return OpenAICompatibleClient()
        except Exception:  # missing SDK, bad key format, etc.
            logger.exception("Could not build the LLM client - falling back to template.")
            return None

    # -- public API -------------------------------------------------------- #

    def compute_targets(self, profile: MaternalProfile) -> NutritionTargets:
        return self.calculator.compute(profile)

    def generate(self, profile: MaternalProfile) -> DietPlan:
        targets = self.compute_targets(profile)
        food_plan = self.food_planner.build(profile, targets)

        if self.llm_client is None:
            return DietPlan(profile, targets, food_plan,
                            self._template_plan(profile, targets, food_plan), "fallback-template")

        messages = self.prompt_builder.build(profile, targets, food_plan)
        try:
            text = self.llm_client.complete(messages)
            if not text:
                raise RuntimeError("Empty response from the model.")
            return DietPlan(profile, targets, food_plan,
                            text + self._total_check(text, targets), self.llm_client.name)
        except Exception:
            logger.exception("LLM call failed - serving the template plan instead.")
            return DietPlan(profile, targets, food_plan,
                            self._template_plan(profile, targets, food_plan), "fallback-template")

    def generate_json(self, payload: Dict[str, Any]) -> str:
        """Convenience entry point for a future FastAPI route."""
        plan = self.generate(MaternalProfile(**payload))
        return json.dumps(plan.to_dict(), indent=2, ensure_ascii=False)

    # -- checks ------------------------------------------------------------ #

    @classmethod
    def _total_check(cls, text: str, targets: NutritionTargets) -> str:
        """Flag a model table whose Total row drifts from the calculated target."""
        match = re.search(r"^\|\s*\**total\**\s*\|(.*)$", text, re.IGNORECASE | re.MULTILINE)
        if not match:
            return ""
        numbers = [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", match.group(1))]
        if not numbers:
            return ""
        kcal = max(numbers)  # kcal is the largest figure in the row
        if abs(kcal - targets.calorie_target) / targets.calorie_target > cls.TOTAL_TOLERANCE:
            return (f"\n\n> **Check:** the menu above adds up to about {kcal:.0f} kcal, but the "
                    f"calculated target is {targets.calorie_target:.0f} kcal. Follow the daily "
                    f"food-group quantities, which are sized to the target.")
        return ""

    # -- fallback ---------------------------------------------------------- #

    @staticmethod
    def _template_plan(profile: MaternalProfile, t: NutritionTargets, fp: FoodPlan) -> str:
        diet = profile.diet_type
        g = fp.groups

        def q(meal: str, group: str) -> float:
            m = next(x for x in fp.meals if x["name"] == meal)
            return m["items"].get(group, 0)

        def roti(grams: float) -> str:
            return f"{max(1, round(grams / 30))} roti"

        pulses_b, pulses_l, pulses_d = q("Breakfast", "Pulses & legumes"), q("Lunch", "Pulses & legumes"), q("Dinner", "Pulses & legumes")
        foods = {
            "Early morning": f"{q('Early morning', 'Nuts & oilseeds'):.0f} g soaked almonds, walnuts or "
                             f"groundnuts + {q('Early morning', 'Milk & curd'):.0f} ml milk",
            "Breakfast": f"{q('Breakfast', 'Cereals & millets'):.0f} g whole grain or millet (ragi dosa, "
                         f"vegetable poha, besan-millet chilla, about {roti(q('Breakfast', 'Cereals & millets'))}) "
                         f"with {pulses_b:.0f} g dal or sprouts and "
                         f"{q('Breakfast', 'Other vegetables') + q('Breakfast', 'Roots & tubers'):.0f} g vegetables; "
                         f"{q('Breakfast', 'Nuts & oilseeds'):.0f} g groundnuts or sesame"
                         + ("; 1 boiled egg" if diet == "Eggetarian" else ""),
            "Mid-morning": f"{q('Mid-morning', 'Fruits'):.0f} g seasonal fruit (guava, orange, banana, ripe papaya)",
            "Lunch": f"{q('Lunch', 'Cereals & millets'):.0f} g rice or atta (about {roti(q('Lunch', 'Cereals & millets'))} "
                     f"or {q('Lunch', 'Cereals & millets') / 60:.1f} katori cooked rice) + {pulses_l:.0f} g dal "
                     f"(about {pulses_l / 30:.1f} katori cooked) "
                     + ("+ 80 g fish or chicken curry " if diet == "Non-Vegetarian" else "")
                     + f"+ {q('Lunch', 'Green leafy vegetables'):.0f} g palak/methi/amaranth sabzi "
                     f"+ {q('Lunch', 'Other vegetables') + q('Lunch', 'Roots & tubers'):.0f} g vegetable curry "
                     f"+ {q('Lunch', 'Milk & curd'):.0f} ml curd",
            "Evening snack": f"{q('Evening snack', 'Milk & curd'):.0f} ml milk or buttermilk + "
                             f"{q('Evening snack', 'Fruits'):.0f} g fruit",
            "Dinner": f"{q('Dinner', 'Cereals & millets'):.0f} g atta/millet (about {roti(q('Dinner', 'Cereals & millets'))}) "
                      f"+ {pulses_d:.0f} g rajma, chana or dal + "
                      f"{q('Dinner', 'Green leafy vegetables'):.0f} g GLV + "
                      f"{q('Dinner', 'Other vegetables') + q('Dinner', 'Roots & tubers'):.0f} g vegetables "
                      f"+ {q('Dinner', 'Fruits'):.0f} g fruit",
        }

        table = ["| Meal | What to eat (raw weights) | Approx. kcal | Approx. protein (g) |",
                 "| --- | --- | --- | --- |"]
        for m in fp.meals:
            table.append(f"| {m['name']} ({m['time']}) | {foods[m['name']]} | {m['kcal']} | {m['protein_g']:.0f} |")
        table.append(f"| **Total** | Cooking oil/ghee across the day: {g['Cooking oil & ghee']:.0f} g "
                     f"({household_measure('Cooking oil & ghee', g['Cooking oil & ghee'])}) | "
                     f"**{fp.kcal:.0f}** | **{fp.protein_g:.0f}** |")

        stage = (f"{profile.effective_trimester}-trimester" if profile.is_pregnant
                 else profile.stage.lower())
        why_energy = (f"- Energy is set at {t.calorie_target:.0f} kcal, including +{t.pregnancy_kcal_addition:.0f} kcal "
                      f"for this stage." if t.pregnancy_kcal_addition else
                      f"- Energy is set at {t.calorie_target:.0f} kcal. No extra calories are needed in the "
                      f"first trimester - the focus is food quality, especially folate.")

        return f"""### Day at a glance
A standard {stage} {diet.lower()} plan built from the ICMR-NIN 2024 food basket and sized to the
calculated targets. Connect an LLM key to get a menu tailored to her notes and preferences.

{chr(10).join(table)}

### Why this plan
{why_energy}
- Protein ({fp.protein_g:.0f} g from these foods, target {t.protein_target_g:.0f} g) comes from dal, milk and curd at every main meal; cereals and pulses are kept at 3:1 for better protein quality.
- {g['Milk & curd']:.0f} ml of milk and curd a day goes most of the way to the {t.micronutrients['Calcium']} calcium need.
- Green leafy vegetables, pulses and a vitamin-C fruit together raise iron and folate absorption from plant foods.
- Use iodised salt, and make half the cereals whole grains or millets (ragi, jowar, bajra).

### Foods to limit
- Alcohol and tobacco in any form.
- Tea and coffee with or just after meals or IFA tablets - they block iron. Keep caffeine low overall.
- Unpasteurised milk, raw or runny eggs, undercooked meat, and food left out for hours.
- High-mercury fish such as shark, swordfish and king mackerel.
- Packaged foods high in fat, sugar and salt; carbonated drinks; hydrogenated fat (vanaspati).

### Simple swaps
- Paneer is costly - soya chunks, roasted chana or groundnuts give similar protein for less.
- No milk at bedtime? A katori of curd or dalia cooked in milk works the same.
- Almonds can be swapped for groundnuts or sesame (til), which are cheaper and rich in protein.

### When to call your doctor
- Vomiting so often that food or water will not stay down.
- Swelling of the face or hands, a severe headache, or blurred vision.
- Bleeding, leaking fluid, or{' the baby moving less than usual.' if profile.is_pregnant else ' fever or a painful, red breast.'}
"""


__all__ = [
    "MaternalProfile",
    "NutritionTargets",
    "FoodPlan",
    "DietPlan",
    "NutritionCalculator",
    "FoodPlanner",
    "PromptBuilder",
    "LLMClient",
    "OpenAICompatibleClient",
    "DietPlanGenerator",
    "household_measure",
    "ACTIVITY_FACTORS",
    "ENERGY_ADDITION",
    "PROTEIN_ADDITION",
    "STAGES",
    "DIET_TYPES",
]
