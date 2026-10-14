from django import template

register = template.Library()


@register.simple_tag
def field(field):
    attributes = field.field.widget.attrs.copy()
    if field.errors:
        error_id = f"{field.id_for_label}_error"
        described_by = attributes.get("aria-describedby", "").split()
        if error_id not in described_by:
            described_by.append(error_id)
        attributes["aria-invalid"] = "true"
        attributes["aria-describedby"] = " ".join(described_by)
    return field.as_widget(attrs=attributes)
