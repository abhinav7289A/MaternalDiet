"""
app.py
------
Gradio front end for the maternal diet planner.

This file only does three things: collect input, call the engine, render output.
All nutrition and LLM logic lives in nutrition_engine.py so the unified app can
import that module directly and ignore this one.

Run locally:  python app.py
Render:       web: python app.py   (see Procfile)
"""

from __future__ import annotations

import html
import logging
import os

from dotenv import load_dotenv

load_dotenv()  # must run before the engine reads LLM_* variables

import gradio as gr  # noqa: E402

from nutrition_engine import (  # noqa: E402
    DIET_TYPES, FOOD_UNITS, STAGES, DietPlan, DietPlanGenerator, MaternalProfile, household_measure,
)

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

APP_TITLE = "Maternal Diet Planner"
APP_SUBTITLE = "Daily energy, protein and meal guidance for pregnancy and breastfeeding"

# One generator for the whole process - the LLM client is built once, not per click.
GENERATOR = DietPlanGenerator()


# --------------------------------------------------------------------------- #
# Visual design
# --------------------------------------------------------------------------- #
# Palette: clinical paper white, deep pine ink, one teal accent for anything
# actionable, amber reserved strictly for safety notices so it never competes
# with ordinary UI colour. Neutrals use Gradio's own variables so dark mode works.

THEME = gr.themes.Base(
    primary_hue=gr.themes.colors.teal,
    neutral_hue=gr.themes.colors.slate,
    font=[gr.themes.GoogleFont("IBM Plex Sans"), "ui-sans-serif", "system-ui", "sans-serif"],
).set(
    body_background_fill="#F4F7F8",
    body_background_fill_dark="#0F1A1A",
    block_background_fill="#FFFFFF",
    block_background_fill_dark="#152322",
    block_border_width="1px",
    block_border_color="#DDE5E5",
    block_border_color_dark="#27403E",
    block_radius="8px",
    block_shadow="none",
    block_label_text_weight="500",
    button_primary_background_fill="#0F766E",
    button_primary_background_fill_hover="#115E59",
    button_primary_text_color="#FFFFFF",
    input_border_color="#CBD8D8",
)

CSS = """
.gradio-container { max-width: 1280px !important; }
#masthead {
    border-bottom: 2px solid #0F766E;
    padding: 4px 0 14px 0;
    margin-bottom: 14px;
}
#masthead h1 { font-size: 1.6rem; font-weight: 600; letter-spacing: -0.01em; margin: 0; }
#masthead p { opacity: .75; margin: 4px 0 0 0; font-size: 0.95rem; }
#masthead .badge {
    display: inline-block; margin-top: 8px; padding: 2px 10px; border-radius: 999px;
    font-size: .75rem; background: rgba(15,118,110,.10); color: #0F766E; border: 1px solid rgba(15,118,110,.25);
}
.section-label {
    font-size: .72rem; font-weight: 600; text-transform: uppercase; letter-spacing: .06em;
    color: #0F766E; margin: 6px 0 -4px 0;
}
.safety-note, .alert {
    background: #FDF6EC; border-left: 3px solid #B45309; padding: 10px 14px;
    font-size: 0.86rem; color: #6B4A16; line-height: 1.5; border-radius: 0 6px 6px 0;
}
.alert { margin-bottom: 8px; }
.alert b { color: #7C2D12; }
.target-grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 1px;
    background: var(--border-color-primary); border: 1px solid var(--border-color-primary);
    border-radius: 8px; overflow: hidden; margin-bottom: 10px;
}
.target-cell { background: var(--block-background-fill); padding: 12px 14px; }
.target-cell.primary { box-shadow: inset 3px 0 0 #0F766E; }
.target-cell .label { font-size: 0.76rem; opacity: .7; margin-bottom: 3px; }
.target-cell .value {
    font-size: 1.4rem; font-weight: 600; font-variant-numeric: tabular-nums; line-height: 1.15;
}
.target-cell .sub { font-size: 0.74rem; opacity: .6; margin-top: 2px; }
.plan-body h3 {
    font-size: 1.02rem; font-weight: 600; border-bottom: 1px solid var(--border-color-primary);
    padding-bottom: 5px; margin-top: 22px;
}
.plan-body table, .data-table { font-size: 0.88rem; width: 100%; border-collapse: collapse; }
.plan-body td, .plan-body th { padding: 7px 9px; }
.data-table th {
    text-align: left; font-weight: 600; font-size: .78rem; opacity: .75;
    border-bottom: 2px solid var(--border-color-primary); padding: 8px;
}
.data-table td { padding: 8px; border-bottom: 1px solid var(--border-color-primary); vertical-align: top; }
.data-table td.num { font-variant-numeric: tabular-nums; white-space: nowrap; }
.data-table tr.total td { font-weight: 600; border-bottom: none; }
.muted { opacity: .65; font-size: .82rem; }
.meal-chip {
    display: inline-block; margin: 2px 4px 2px 0; padding: 1px 8px; border-radius: 999px;
    font-size: .76rem; border: 1px solid var(--border-color-primary);
}
.bar { height: 8px; border-radius: 4px; background: var(--border-color-primary); overflow: hidden; margin-top: 6px; }
.bar > span { display: block; height: 100%; background: #0F766E; }
.card { border: 1px solid var(--border-color-primary); border-radius: 8px; padding: 12px 14px; margin-bottom: 10px; }
.card h4 { margin: 0 0 6px 0; font-size: .95rem; }
.card ul { margin: 4px 0 0 18px; padding: 0; }
.card li { margin-bottom: 4px; font-size: .88rem; line-height: 1.45; }
.trace-line { font-size: 0.85rem; padding: 3px 0; font-variant-numeric: tabular-nums; }
.source-list li { font-size: .82rem; margin-bottom: 4px; }
footer { display: none !important; }
@media (prefers-reduced-motion: reduce) {
    * { animation: none !important; transition: none !important; }
}
"""

SAFETY_NOTE = (
    '<div class="safety-note">General nutrition guidance based on ICMR-NIN recommendations. It does '
    'not replace antenatal care and cannot detect gestational diabetes, anaemia, high blood pressure or '
    'any other condition. Share the plan with your doctor, ANM or dietitian before following it, '
    'especially with a medical condition or a twin pregnancy.</div>'
)

PLACEHOLDER = (
    "Fill in the details on the left and select **Create meal plan**. "
    "Targets are calculated first from ICMR-NIN rules, then the plan is written around them."
)
EMPTY = '<p class="muted">Create a plan to see this section.</p>'

SOURCES_HTML = """
<ul class="source-list">
<li><b>NIN-2020</b> - ICMR-NIN Expert Group, <i>Nutrient Requirements for Indians: RDA and EAR</i>, 2020
(energy: FAO/WHO/UNU BMR less 9% for women x PAL 1.4 / 1.8 / 2.3; protein 0.83 g/kg + 9.5 / 22 g;
micronutrient RDA and EAR tables). <a href="https://www.nin.res.in/rdabook/brief_note.pdf" target="_blank">Brief note (PDF)</a></li>
<li><b>DGI-2024</b> - ICMR-NIN, <i>Dietary Guidelines for Indians</i>, 2024, Guideline 2: pregnancy and lactation
(+350 kcal from the 2nd trimester, +100 for undernourished women, weight-gain targets, food basket,
IFA from week 12). <a href="https://www.nin.res.in/dietaryguidelines/pdfjs/locale/DGI_2024.pdf" target="_blank">PDF</a></li>
<li><b>MoHFW-Ca</b> - MoHFW / NHSRC, <i>National Guidelines for Calcium Supplementation during Pregnancy and
Lactation</i>. <a href="https://nhsrcindia.org/sites/default/files/2021-03/Guidelines%20for%20Calcium%20Supplementation%20during%20Pregnancy%20and%20Lactation.pdf" target="_blank">PDF</a></li>
<li><b>IOM-2009</b> - Institute of Medicine, <i>Weight Gain During Pregnancy</i> (only the 12.5-18 kg range for
underweight women, which DGI-2024 does not give).</li>
</ul>
"""


# --------------------------------------------------------------------------- #
# Rendering helpers
# --------------------------------------------------------------------------- #

def _pct(part_kcal: float, total_kcal: float) -> str:
    return f"{100 * part_kcal / total_kcal:.0f}%" if total_kcal else "-"


def _targets_html(plan: DietPlan) -> str:
    t, fp = plan.targets, plan.food_plan
    addition = (f"{t.maintenance_kcal:.0f} + {t.pregnancy_kcal_addition:.0f} for this stage"
                if t.pregnancy_kcal_addition else f"{t.maintenance_kcal:.0f}, no addition yet")
    cells = [
        ("Daily energy", f"{t.calorie_target:.0f} kcal", addition, True),
        ("Daily protein", f"{t.protein_target_g:.0f} g",
         f"RDA; average need {t.protein_ear_g:.0f} g", True),
        ("Fat", f"{fp.fat_g:.0f} g", f"{_pct(fp.fat_g * 9, fp.kcal)} of energy (20-35%)", False),
        ("Carbohydrate", f"{fp.carb_g:.0f} g", f"{_pct(fp.carb_g * 4, fp.kcal)} of energy (45-65%)", False),
        ("Fibre", f"{fp.fibre_g:.0f} g", f"at least {t.fibre_target_g:.0f} g", False),
        ("Fluids", t.water_litres.split(",")[0], "water and other drinks", False),
        ("BMI", f"{t.bmi:.1f}", t.bmi_category + (" (before pregnancy)" if plan.profile.is_pregnant else ""), False),
    ]
    body = "".join(
        f'<div class="target-cell{" primary" if primary else ""}"><div class="label">{label}</div>'
        f'<div class="value">{value}</div><div class="sub">{html.escape(sub)}</div></div>'
        for label, value, sub, primary in cells
    )
    return f'<div class="target-grid">{body}</div>'


def _alerts_html(plan: DietPlan) -> str:
    return "".join(f'<div class="alert"><b>Note:</b> {html.escape(a)}</div>' for a in plan.targets.alerts)


def _basket_html(plan: DietPlan) -> str:
    fp = plan.food_plan
    rows = []
    for group, qty in fp.groups.items():
        unit = FOOD_UNITS.get(group, "g")
        meals = "".join(f'<span class="meal-chip">{m["name"]} {m["items"][group]:.0f}</span>'
                        for m in fp.meals if group in m["items"])
        rows.append(f"<tr><td>{group}</td><td class='num'>{qty:.0f} {unit}</td>"
                    f"<td class='muted'>{household_measure(group, qty)}</td><td>{meals}</td></tr>")
    meal_rows = "".join(
        f"<tr><td>{m['name']} <span class='muted'>({m['time']})</span></td>"
        f"<td class='num'>{m['kcal']} kcal</td><td class='num'>{m['protein_g']:.0f} g</td>"
        f"<td><div class='bar'><span style='width:{100 * m['kcal'] / fp.kcal:.0f}%'></span></div></td></tr>"
        for m in fp.meals
    )
    return f"""
<p class="muted">Raw weights per day from the ICMR-NIN 2024 food basket. Cereal and pulse amounts are
resized (kept at 3:1) so the day meets the energy target. Make half the cereals whole grains and include
millets such as ragi, jowar or bajra.</p>
<table class="data-table">
<tr><th>Food group</th><th>Per day</th><th>Roughly</th><th>Split across meals (g / ml)</th></tr>
{''.join(rows)}
<tr class="total"><td>This basket provides</td><td class="num">{fp.kcal:.0f} kcal</td>
<td colspan="2">{fp.protein_g:.0f} g protein · {fp.fat_g:.0f} g fat · {fp.carb_g:.0f} g carbohydrate · {fp.fibre_g:.0f} g fibre</td></tr>
</table>
<h4 style="margin-top:18px">Energy by meal</h4>
<table class="data-table">
<tr><th>Meal</th><th>Energy</th><th>Protein</th><th style="width:40%">Share of the day</th></tr>
{meal_rows}
</table>"""


def _micro_html(plan: DietPlan) -> str:
    rows = "".join(
        f"<tr><td>{m['nutrient']}</td><td class='num'><b>{m['rda']:g}</b> {m['unit']}</td>"
        f"<td class='num'>{m['ear']:g} {m['unit']}</td><td class='muted'>{html.escape(m['sources'])}</td></tr>"
        for m in plan.targets.micronutrient_table
    )
    return f"""
<p class="muted">RDA is the intake that covers almost everyone (97.5%); EAR is the average need.
ICMR-NIN 2020 values for this stage{', using the higher adolescent value where it applies' if plan.profile.age < 19 else ''}.</p>
<table class="data-table">
<tr><th>Nutrient</th><th>RDA / day</th><th>EAR / day</th><th>Good food sources</th></tr>
{rows}
</table>"""


def _care_html(plan: DietPlan) -> str:
    t, p = plan.targets, plan.profile
    supp = "".join(f"<li>{html.escape(s)}</li>" for s in t.supplements)
    parts = [f'<div class="card"><h4>Government supplement schedule</h4>'
             f'<p class="muted">Reminders only. Take what your doctor or ANM prescribes; these tablets are '
             f'free at government health centres.</p><ul>{supp}</ul></div>']
    if t.weight_gain_range_kg:
        lo, hi = t.weight_gain_range_kg
        so_far = ""
        if t.weight_gain_so_far_kg is not None:
            width = max(0, min(100, 100 * t.weight_gain_so_far_kg / hi))
            so_far = (f"<p>Gained so far: <b>{t.weight_gain_so_far_kg:g} kg</b> "
                      f"(week {p.gestational_week or '-'}).</p>"
                      f"<div class='bar'><span style='width:{width:.0f}%'></span></div>"
                      f"<p class='muted'>Bar shows gain so far against the upper end of the range. "
                      f"Most of the gain comes in the 2nd and 3rd trimesters.</p>")
        parts.append(f'<div class="card"><h4>Weight gain over the whole pregnancy</h4>'
                     f'<p>Recommended for a {t.bmi_category.lower()} BMI before pregnancy: '
                     f'<b>{lo:g}-{hi:g} kg</b>.</p>{so_far}</div>')
    parts.append('<div class="card"><h4>Everyday habits (DGI-2024)</h4><ul>'
                 '<li>Eat small, frequent meals (4-6 a day) if there is nausea or vomiting.</li>'
                 '<li>Keep tea and coffee away from meals and IFA tablets.</li>'
                 '<li>15 minutes of direct sunlight a day for vitamin D.</li>'
                 '<li>At least four antenatal check-ups: weight, blood pressure, haemoglobin.</li>'
                 '<li>Rest 2-3 hours during the day; avoid heavy lifting.</li>'
                 '<li>Collect supplementary nutrition from the Anganwadi centre if eligible.</li>'
                 '</ul></div>')
    return "".join(parts)


def _trace_html(plan: DietPlan) -> str:
    lines = "".join(f'<div class="trace-line">{html.escape(line)}</div>' for line in plan.targets.calculation_trace)
    return lines + "<h4 style='margin-top:16px'>Sources</h4>" + SOURCES_HTML


# --------------------------------------------------------------------------- #
# Event handlers
# --------------------------------------------------------------------------- #

def _num(value):
    """Optional number field; Gradio sends 0 for an empty gr.Number."""
    return float(value) if value else None


def create_plan(stage, week, age, height, weight, current_weight, activity, diet_type, notes):
    """Returns (targets, alerts, plan, basket, micronutrients, care, trace)."""
    blank = ("", "", None, EMPTY, EMPTY, EMPTY, EMPTY)
    try:
        pregnant = stage == "Pregnant"
        profile = MaternalProfile(
            age=int(age),
            weight_kg=float(weight),
            height_cm=float(height),
            activity_level=activity,
            diet_type=diet_type,
            stage=stage,
            gestational_week=int(week) if pregnant else None,
            current_weight_kg=_num(current_weight) if pregnant else None,
            notes=notes or "",
        )
    except (TypeError, ValueError):
        return ("", "", "Enter age, height and weight as numbers before continuing.") + blank[3:]

    try:
        plan = GENERATOR.generate(profile)
    except ValueError as exc:  # validation failures carry a patient-friendly message
        return ("", "", str(exc)) + blank[3:]
    except Exception:
        logger.exception("Plan generation failed")
        return ("", "", "The plan could not be created just now. Try again in a moment.") + blank[3:]

    note = ""
    if plan.source == "fallback-template":
        note = ("\n\n---\n*Written from the built-in template because no language model is "
                "connected. The calculated targets and food basket are unaffected.*")
    else:
        note = ("\n\n---\n*Menu written by a language model from the calculated food basket. "
                "If a dish does not suit her, swap it within the same food group.*")

    return (_targets_html(plan), _alerts_html(plan), plan.plan_markdown + note, _basket_html(plan),
            _micro_html(plan), _care_html(plan), _trace_html(plan))


def on_stage_change(stage):
    pregnant = stage == "Pregnant"
    return (
        gr.update(visible=pregnant),
        gr.update(visible=pregnant),
        gr.update(label="Weight before pregnancy (kg)" if pregnant else "Current weight (kg)",
                  info="Or weight at the first antenatal visit" if pregnant else None),
    )


def week_hint(week):
    week = int(week or 1)
    tri = "1st" if week <= 13 else "2nd" if week <= 27 else "3rd"
    return f"<span class='muted'>Week {week} is in the <b>{tri} trimester</b>.</span>"


# --------------------------------------------------------------------------- #
# Interface
# --------------------------------------------------------------------------- #

DEFAULTS = dict(stage="Pregnant", week=20, age=27, height=158, weight=55, current_weight=None,
                activity="Moderate", diet="Vegetarian", notes="")


def build_interface() -> gr.Blocks:
    # Gradio 6 takes theme and css at launch() time, not in the Blocks constructor.
    with gr.Blocks(title=APP_TITLE, analytics_enabled=False) as demo:
        gr.HTML(f'<div id="masthead"><h1>{APP_TITLE}</h1><p>{APP_SUBTITLE}</p>'
                f'<span class="badge">Based on ICMR-NIN RDA 2020 and Dietary Guidelines for Indians 2024</span></div>')

        with gr.Row(equal_height=False):
            with gr.Column(scale=2, min_width=300):
                gr.HTML('<div class="section-label">Stage</div>')
                stage = gr.Radio(list(STAGES), value=DEFAULTS["stage"], label="Pregnant or breastfeeding")
                week = gr.Slider(1, 42, value=DEFAULTS["week"], step=1, label="Week of pregnancy",
                                 info="Count from the first day of the last period")
                week_info = gr.HTML(week_hint(DEFAULTS["week"]))

                gr.HTML('<div class="section-label">Body</div>')
                with gr.Row():
                    age = gr.Number(label="Age (years)", value=DEFAULTS["age"], minimum=15, maximum=50, precision=0)
                    height = gr.Number(label="Height (cm)", value=DEFAULTS["height"], minimum=130, maximum=200)
                with gr.Row():
                    weight = gr.Number(label="Weight before pregnancy (kg)", value=DEFAULTS["weight"],
                                       minimum=30, maximum=150, info="Or weight at the first antenatal visit")
                    current_weight = gr.Number(label="Current weight (kg, optional)", value=None,
                                               minimum=30, maximum=190, info="To track weight gain")

                gr.HTML('<div class="section-label">Daily life</div>')
                activity = gr.Radio(
                    ["Sedentary", "Moderate", "Heavy"], value=DEFAULTS["activity"], label="Activity level",
                    info="Sedentary: mostly seated. Moderate: housework and walking. Heavy: field or manual work.",
                )
                diet_type = gr.Radio(list(DIET_TYPES), value=DEFAULTS["diet"], label="Diet type")
                notes = gr.Textbox(
                    label="Anything else to account for",
                    placeholder="Allergies, foods she avoids, regional cuisine, budget",
                    lines=2,
                )
                with gr.Row():
                    clear = gr.Button("Reset", variant="secondary", scale=1)
                    submit = gr.Button("Create meal plan", variant="primary", scale=2)
                gr.HTML(SAFETY_NOTE)

            with gr.Column(scale=3, min_width=380):
                targets_out = gr.HTML()
                alerts_out = gr.HTML()
                with gr.Tabs():
                    with gr.Tab("Meal plan"):
                        plan_out = gr.Markdown(PLACEHOLDER, elem_classes="plan-body")
                    with gr.Tab("Food basket"):
                        basket_out = gr.HTML(EMPTY)
                    with gr.Tab("Vitamins & minerals"):
                        micro_out = gr.HTML(EMPTY)
                    with gr.Tab("Supplements & weight"):
                        care_out = gr.HTML(EMPTY)
                    with gr.Tab("How it's calculated"):
                        trace_out = gr.HTML(EMPTY)

        inputs = [stage, week, age, height, weight, current_weight, activity, diet_type, notes]
        outputs = [targets_out, alerts_out, plan_out, basket_out, micro_out, care_out, trace_out]

        stage.change(on_stage_change, inputs=stage, outputs=[week, current_weight, weight])
        stage.change(lambda s: gr.update(visible=s == "Pregnant"), inputs=stage, outputs=week_info)
        week.change(week_hint, inputs=week, outputs=week_info)
        submit.click(create_plan, inputs=inputs, outputs=outputs, api_name="create_plan")
        clear.click(
            lambda: (DEFAULTS["stage"], DEFAULTS["week"], DEFAULTS["age"], DEFAULTS["height"],
                     DEFAULTS["weight"], DEFAULTS["current_weight"], DEFAULTS["activity"],
                     DEFAULTS["diet"], DEFAULTS["notes"],
                     "", "", PLACEHOLDER, EMPTY, EMPTY, EMPTY, EMPTY),
            outputs=inputs + outputs,
        )

    return demo


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    # Render needs 0.0.0.0 to route traffic in (it sets RENDER=true). Locally use
    # 127.0.0.1, because browsers cannot open the 0.0.0.0 link Gradio prints.
    host = os.environ.get("SERVER_NAME") or ("0.0.0.0" if os.environ.get("RENDER") else "127.0.0.1")
    build_interface().queue(max_size=20).launch(
        server_name=host,
        server_port=port,
        theme=THEME,
        css=CSS,
    )
