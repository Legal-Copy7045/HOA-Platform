"""Starter list of project kinds, so owners always pick from a list.

Added only when the board has none yet; the board edits or retires them on the
ARC request types page. Guidance is generic: the board cites its own guidelines.
"""

from django.db import migrations

STARTERS = [
    ("Fence", "Plot plan showing the fence line, height, style, material and color."),
    ("Deck or patio", "Plot plan, dimensions, materials and color; drawings for raised decks."),
    ("Shed or outbuilding", "Plot plan with setbacks, dimensions, materials and colors."),
    ("Addition or porch", "Drawings, plot plan with setbacks, materials and colors."),
    ("Exterior paint or color change", "Color samples or names for body, trim and doors."),
    ("Roof", "Material, style and color."),
    ("Windows or doors", "Style, material and color."),
    ("Driveway or walkway", "Plot plan, dimensions and material."),
    ("Landscaping or tree removal", "Plan showing plants or trees added or removed."),
    ("Pool or hot tub", "Plot plan with setbacks, fencing and equipment location."),
    ("Solar panels", "Panel layout on the roof and equipment location."),
    ("Play equipment", "Plot plan, dimensions and materials."),
    ("Other", "Describe the project and attach drawings or photos."),
]


def add_starters(apps, schema_editor):
    ArcProjectType = apps.get_model("arc", "ArcProjectType")
    if ArcProjectType.objects.exists():
        return
    for i, (name, guidance) in enumerate(STARTERS):
        ArcProjectType.objects.create(name=name, guidance=guidance, sort_order=(i + 1) * 10)


class Migration(migrations.Migration):
    dependencies = [("arc", "0005_arc_form_fields")]

    operations = [migrations.RunPython(add_starters, migrations.RunPython.noop)]
