import builtins
import json
import re
from django import template

register = template.Library()


@register.filter(name='zip')
def zip_filter(a, b):
    return builtins.zip(a, b)


@register.filter(name='get_item')
def get_item(d, key):
    if isinstance(d, dict):
        return d.get(key, [])
    return []


@register.filter(name='shift_css_class')
def shift_css_class(shift_value):
    """Return CSS class for a shift value based on start time."""
    NON_WORKING = {'OFF', 'H', 'SICK', 'Comp', 'Paternity', 'Maternity', 'TBC', 'OFF/R', ''}
    if not shift_value or shift_value in NON_WORKING:
        if shift_value == 'H':
            return 'shift-h'
        if shift_value == 'SICK':
            return 'shift-sick'
        if shift_value in ('Comp', 'Paternity', 'Maternity', 'TBC', 'OFF/R'):
            return 'shift-other'
        return 'shift-off'
    m = re.match(r'^(\d{4})-', str(shift_value))
    if not m:
        return 'shift-other'
    start = int(m.group(1))
    if start < 800:
        return 'shift-early'
    elif start < 1200:
        return 'shift-am'
    elif start < 1800:
        return 'shift-pm'
    elif start < 2200:
        return 'shift-late'
    else:
        return 'shift-night'


@register.filter(name='section_css_class')
def section_css_class(section):
    """Return CSS class for section row background."""
    name = (section.name or '').upper()
    if 'EXECUTIVE' in name:
        return 'sec-row-EXECUTIVE-CHEF'
    if 'CONFERENCE' in name:
        return 'sec-row-CONFERENCE'
    if 'DUTY' in name:
        return 'sec-row-DUTY'
    if 'SAUCE' in name:
        return 'sec-row-SAUCE'
    if 'GARNISH' in name:
        return 'sec-row-GARNISH'
    if 'LARDER' in name:
        return 'sec-row-LARDER'
    if 'BREAKFAST' in name:
        return 'sec-row-BREAKFAST'
    if 'NIGHT' in name:
        return 'sec-row-NIGHT'
    if 'CANTE' in name or 'CAFE' in name or 'CAFÉ' in name:
        return 'sec-row-CANTEEN'
    if 'PASTRY' in name or 'BAKERY' in name:
        return 'sec-row-PASTRY'
    if 'TERRACE' in name:
        return 'sec-row-TERRACE'
    return 'section-row'


@register.filter(name='chef_json')
def chef_json(chef):
    """Return a chef object as a JSON-safe string for JS."""
    return json.dumps({
        'id': chef.id,
        'name': chef.name,
        'role': chef.role,
        'section_id': chef.section_id,
        'employment_type': chef.employment_type,
        'contracted_hours_pw': float(chef.contracted_hours_pw),
        'email': chef.email,
        'phone': chef.phone,
        'start_date': chef.start_date.isoformat() if chef.start_date else '',
        'end_date': chef.end_date.isoformat() if chef.end_date else '',
        'is_active': chef.is_active,
    })


@register.filter(name='section_color')
def section_color_filter(section_or_name):
    """Filter version: {{ section|section_color }} — works on Section object or string."""
    name = getattr(section_or_name, 'name', section_or_name) or ''
    return _get_section_color(name)


def _get_section_color(name):
    COLORS = {
        'EXECUTIVE': '#FFD966',
        'CONFERENCE': '#00B050',
        'DUTY': '#AEAAAA',
        'SAUCE': '#FF0000',
        'GARNISH': '#B4C6E7',
        'LARDER': '#92D050',
        'BREAKFAST': '#FFE699',
        'NIGHT': '#333333',
        'CANTE': '#808080',
        'CAFE': '#808080',
        'PASTRY': '#00B0F0',
        'TERRACE': '#F4B084',
    }
    uname = (name or '').upper()
    for keyword, color in COLORS.items():
        if keyword in uname:
            return color
    return '#888888'


@register.simple_tag
def section_color(section_name):
    """Return hex colour for a section."""
    return _get_section_color(section_name)
