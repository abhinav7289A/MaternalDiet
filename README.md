# Maternal Diet Planner

A standalone Gradio module that calculates energy, protein and micronutrient targets for
pregnancy and breastfeeding with deterministic ICMR-NIN rules, builds a daily food basket that
meets them, then asks an LLM to turn that basket into a one-day Indian menu.

    app.py                    Gradio UI only
    nutrition_engine.py       rules, prompt, LLM client, orchestration (no Gradio import)
    test_nutrition_engine.py  rule-layer tests
    requirements.txt
    Procfile

## Run locally

    python -m venv venv
    venv\Scripts\activate            # Windows  (macOS/Linux: source venv/bin/activate)
    pip install -r requirements.txt
    copy env.example .env            # then put your key in LLM_API_KEY (optional)
    python app.py                    # http://localhost:7860
    python -m pytest -q              # rule-layer tests (pip install pytest)

Without `LLM_API_KEY` the app still works and writes the menu from a built-in template.
`.env` is loaded automatically at startup.

## Deploy on Render

1. Push this folder to a GitHub repo (`.env` and `venv/` are git-ignored, so the key stays local).
2. Render dashboard > New > Blueprint > pick the repo. `render.yaml` sets the build/start
   commands, Python 3.11.4 and the Groq settings.
3. When asked, paste your Groq key into `LLM_API_KEY`, then Apply.
4. `PORT` and `RENDER` are injected by Render; `app.py` then binds to `0.0.0.0:$PORT`.

The free tier sleeps after inactivity, so the first request after idling takes
about 50 seconds. Anything user-facing should sit on a paid instance.

## Reusing the engine in the unified app

`app.py` is disposable. The unified app imports the engine directly:

    from nutrition_engine import MaternalProfile, DietPlanGenerator

    generator = DietPlanGenerator()          # build once at startup

    @router.post("/maternal/diet")
    def diet(payload: DietRequest):
        plan = generator.generate(MaternalProfile(**payload.dict()))
        return plan.to_dict()

To change provider, pass a different `LLMClient` into `DietPlanGenerator(llm_client=...)`.
Nothing else changes.

## Nutrition rules

All figures are from Government of India sources; the code cites each one inline.

| What | Rule | Source |
|---|---|---|
| BMR | FAO/WHO/UNU 2004 female equations, less 9% for adult Indian women (no reduction under 19 y) | ICMR-NIN RDA 2020 |
| Activity factor | 1.4 sedentary / 1.8 moderate / 2.3 heavy (reproduces 1660 / 2130 / 2720 kcal at 55 kg) | ICMR-NIN RDA 2020 |
| Body weight used | Pre-pregnancy weight (or first ANC weight); current weight when breastfeeding | ICMR-NIN RDA 2020 ("55 + GWG") |
| Energy addition | 1st trimester 0; 2nd and 3rd +350 kcal (+100 more if BMI < 18.5); lactation +600 / +520 | RDA 2020, DGI 2024 |
| Protein | 0.83 g/kg (EAR 0.66) + 9.5 g (2nd) / 22 g (3rd); lactation +17 / +13 g | ICMR-NIN RDA 2020 |
| Micronutrients | Full RDA and EAR tables for pregnancy and lactation; adolescents get the higher of age and stage values | ICMR-NIN RDA 2020 |
| Fat / carbohydrate | Fat ~30% of energy within AMDR 20-35%; carbohydrate 45-65% | RDA 2020, DGI 2024 |
| Fibre, fluids | 30 g per 2000 kcal; 2.1-3.2 L fluids in pregnancy | ICMR-NIN RDA 2020 |
| BMI and weight gain | Cut-offs 18.5 / 23 / 27.5; gain 10-12 kg (normal), 5-9 kg (overweight/obese), 12.5-18 kg (underweight, IOM 2009) | DGI 2024 |
| Food basket | DGI daily basket for pregnant / lactating / 1st trimester; cereals and pulses resized at 3:1 to meet energy; 30 g pulses swapped for 1 egg or 80 g fish/chicken | DGI 2024 Table 1.6, Fig 2.2-2.3 |
| Supplements | Folic acid 0.5 mg to week 12; IFA (60 mg Fe + 0.5 mg FA) from week 12; calcium 2 x 500 mg + D3 from week 14 | DGI 2024, MoHFW calcium guidelines |

The 55 kg sedentary 3rd-trimester basket comes to ~2010 kcal with 215 g cereals and 70 g pulses,
against DGI 2024's own chart of ~2020 kcal, 220 g and 75 g. The tests check this.

Not a medical device. General guidance only, and no substitute for antenatal care.
