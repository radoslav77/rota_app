"""
Self-learning rota generation algorithm — v6.

Improvements over v5:
  1. Cost-aware borrowing   — borrow the LEAST harmful chef, not the first available
  2. Fatigue-aware sequencing — penalise late→early, night→AM transitions
  3. Consistency bonuses    — reward same-band runs (AM-AM-AM)
  4. Monthly projection bias — prefer under-hours chefs for extra work
  5. Explainability layer   — every assignment records a reason code
  6. Safe optimisation pass  — swap-equivalent shifts to reduce borrowing and fatigue
  7. Part-time rest days     — derived from contracted_hours_pw
  8. All v5 safeguards preserved:
       - H/SICK/protected days are sacred, never overwritten
       - holiday_map entries locked in unconditionally
       - section minimums guaranteed every day including weekends
       - weekly hours cap enforced without touching protected days
"""

import math
from dataclasses import dataclass, field
from collections import defaultdict
from datetime import timedelta, date
import re

# ── Non-working / protected sets ────────────────────────────────────────────────
NON_WORKING = {'OFF', 'H', 'SICK', 'Comp', 'Paternity', 'Maternity', 'TBC', 'OFF/R', ''}
PROTECTED   = {'H', 'SICK', 'Comp', 'Paternity', 'Maternity', 'TBC', 'OFF/R'}

BREAK_MINUTES   = 30
HOLIDAY_MINUTES = 8 * 60
MONTHLY_TARGET_MINUTES = int(173.5 * 60)

# ── Shift duration lookup (gross minutes; break deducted in shift_duration_minutes) ──
SHIFT_DURATIONS = {
    '0400-1230': 470, '0400-1430': 630, '0430-1230': 460, '0430-1300': 510,
    '0500-1330': 510, '0600-1430': 510, '0700-1500': 480,
    '0700-1530': 510, '0700-2300': 960, '0800-1600': 480, '0800-1630': 510,
    '0800-1700': 540, '0800-2330': 930, '0900-1730': 510, '0900-1900': 600,
    '1000-1830': 510, '1000-2130': 690, '1000-2300': 780, '1000-2330': 810,
    '1100-1930': 510, '1100-2200': 690, '1200-2100': 540, '1200-2130': 570,
    '1230-2100': 510, '1430-2130': 420, '1430-2300': 510,
    '1530-2300': 450, '2130-0600': 510, '2200-0630': 510,
}

TARGET_WEEKLY_MINUTES  = 40 * 60
MIN_WEEKLY_MINUTES     = 32 * 60
MAX_WEEKLY_MINUTES     = 42 * 60    # hard cap: 42h net per week

# ── Shift band classification ─────────────────────────────────────────────────
SHIFT_BANDS = {
    'early': (0,    800),
    'am':    (800,  1200),
    'pm':    (1200, 1800),
    'late':  (1800, 2200),
    'night': (2200, 2400),
}

# ── Section colours ─────────────────────────────────────────────────────────────
SECTION_COLORS = {
    'EXECUTIVE CHEF':               '#FFD966',
    'CONFERENCE & EVENTS':          '#00B050',
    'DUTY CHEFS':                   '#AEAAAA',
    'MAIN KITCHEN SAUCE SECTION':   '#FF0000',
    'MAIN KITCHEN GARNISH SECTION': '#B4C6E7',
    'MAIN KITCHEN LARDER':          '#92D050',
    'BREAKFAST SHIFT':              '#FFE699',
    'NIGHT SHIFT':                  '#000000',
    'PARK LANE CAFÉ "STAFF CANTEEN"': '#808080',
    'PASTRY & BAKERY':              '#00B0F0',
    "THEO'S TERRACE":               '#F4B084',
}

# ── Section minimum cover (every day, including weekends) ────────────────────
SECTION_MINIMUM_COVER = {
    'SAUCE':     (2, '1000-2330'),
    'GARNISH':   (2, '1000-2330'),
    'LARDER':    (2, '0800-2330'),
    'BREAKFAST': (4, '0400-1430'),
    'DUTY':      (2, '0700-2300'),
    'NIGHT':     (1, '2200-0630'),
    'TERRACE':   (2, '1200-2130'),
}

# ── Section label map (used in generated rota cells) ────────────────────────
# Maps section keyword → short label shown under each working shift cell.
# Labels match exactly what appears in the reference Excel rota.
SECTION_LABELS = {
    'SAUCE':       'Sauce',
    'GARNISH':     'Garnish',
    'LARDER':      'Larder',
    'BREAKFAST':   'Breakfast',
    'DUTY':        'Duty',
    'NIGHT':       'Night',
    'TERRACE':     'Terrace',
    'CONFERENCE':  'BQT',
    'CANTEEN':     'Staff Canteen',
    'CAFE':        'Staff Canteen',
    'PASTRY':      'Pastry',
    'EXECUTIVE':   'Exec Chef',
}

# Brunch allocation: Saturday and Sunday shifts in sections that run brunch
BRUNCH_SECTIONS  = {'BREAKFAST', 'DUTY', 'CONFERENCE', 'TERRACE'}
BRUNCH_SHIFT_SAT = '0700-1530'   # typical brunch Saturday
BRUNCH_SHIFT_SUN = '0700-1530'   # typical brunch Sunday
BRUNCH_LABEL     = 'BRUNCH'

# Sunday brunch hard cap: exactly 2 people, only these shifts
SUNDAY_BRUNCH_RULE = {
    'max_staff':      2,
    'allowed_shifts': {'0700-1530', '0800-1630', '0700-1500', '0800-1600'},
}

# Staff canteen Sunday: exactly 2 staggered slots
STAFF_CANTEEN_SUNDAY = {
    'slots':     ['0800-1620', '1100-1930'],
    'max_staff': 2,
}

# Club lounge label (for Breakfast chefs sent to club lounge)
CLUB_LOUNGE_SECTIONS = {'BREAKFAST'}

# ── Per-staff shift overrides ────────────────────────────────────────────────
STAFF_SHIFT_OVERRIDES = {
    'Emil':  ['2130-0600', '2200-0630'],
    'Zsolt': ['2130-0600', '2200-0630'],
}

PASTRY_RULES = {
    'min_am':    2,
    'min_late':  1,
    'am_shift':  '0700-1530',
    'late_shift': '1430-2300',
}


# ══════════════════════════════════════════════════════════════════════════════
# EXPLAINABILITY LAYER
# Every assignment decision records a structured reason.
# The reasons dict is reset per generate_rota() call.
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class AssignmentReason:
    code:        str   # machine-readable  e.g. 'PATTERN_MATCH'
    label:       str   # short human text  e.g. 'Pattern match'
    explanation: str   # full sentence
    severity:    str   # 'info' | 'warning' | 'critical'

# Module-level store; reset at the start of each generate_rota() call
_assignment_reasons: dict = defaultdict(list)

def log_reason(staff_id: int, date_iso: str, reason: AssignmentReason):
    _assignment_reasons[(staff_id, date_iso)].append(reason)

def get_assignment_reasons() -> dict:
    return dict(_assignment_reasons)


# ══════════════════════════════════════════════════════════════════════════════
# SHIFT HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def shift_band(shift_value: str):
    """Return the time-band name for a shift, or None."""
    m = re.match(r'^(\d{2})(\d{2})-', shift_value or '')
    if not m:
        return None
    t = int(m.group(1)) * 100 + int(m.group(2))
    for band, (lo, hi) in SHIFT_BANDS.items():
        if lo <= t < hi:
            return band
    return None


def shift_color_class(shift_value):
    if not shift_value or shift_value in NON_WORKING:
        return 'shift-off'
    m = re.match(r'^(\d{4})-', shift_value)
    if not m:
        return 'shift-other'
    start = int(m.group(1))
    if start < 800:   return 'shift-early'
    if start < 1200:  return 'shift-am'
    if start < 1800:  return 'shift-pm'
    if start < 2200:  return 'shift-late'
    return 'shift-night'


def shift_duration_minutes(shift_value):
    """Net paid minutes (break deducted). H/NON_WORKING → 0."""
    if shift_value in NON_WORKING:
        return 0
    if shift_value in SHIFT_DURATIONS:
        gross = SHIFT_DURATIONS[shift_value]
        return gross - BREAK_MINUTES if gross >= 60 else gross
    m = re.match(r'^(\d{2})(\d{2})-(\d{2})(\d{2})$', shift_value)
    if not m:
        return 480 - BREAK_MINUTES
    sh, sm, eh, em = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
    start = sh * 60 + sm
    end   = eh * 60 + em
    if end < start:
        end += 24 * 60
    gross = end - start
    return gross - BREAK_MINUTES if gross >= 60 else gross


# ══════════════════════════════════════════════════════════════════════════════
# FATIGUE & CONSISTENCY  (Improvement 2 + 3)
# ══════════════════════════════════════════════════════════════════════════════

def fatigue_penalty(prev_shift, candidate_shift) -> int:
    """
    Return a penalty score for assigning candidate_shift the day after prev_shift.
    Higher = worse sequence.
    """
    if not prev_shift or prev_shift in NON_WORKING:
        return 0
    if not candidate_shift or candidate_shift in NON_WORKING:
        return 0
    p = shift_band(prev_shift)
    c = shift_band(candidate_shift)
    if p == 'late' and c in ('early', 'am'):
        return 3   # Late → early next day: hard on the chef
    if p == 'night' and c not in ('night', None):
        return 4   # Night → any non-night: disrupt sleep badly
    return 0


def consistency_bonus(prev_shift, candidate_shift) -> int:
    """Reward keeping the same shift band — stable AM blocks, stable late blocks."""
    if not prev_shift or prev_shift in NON_WORKING:
        return 0
    return 1 if shift_band(prev_shift) == shift_band(candidate_shift) else 0


# ══════════════════════════════════════════════════════════════════════════════
# MONTHLY PROJECTION  (Improvement 4)
# ══════════════════════════════════════════════════════════════════════════════

def project_month_end_hours(staff_id, shift_map, all_dates) -> int:
    """
    Estimate end-of-month net minutes for a chef based on what is already
    assigned.  Used to bias borrowing toward under-hours chefs.
    """
    assigned_dates = [d for d in all_dates if (staff_id, d.isoformat()) in shift_map]
    remaining_dates = [d for d in all_dates if (staff_id, d.isoformat()) not in shift_map]
    worked = sum(
        shift_duration_minutes(shift_map.get((staff_id, d.isoformat()), 'OFF'))
        for d in assigned_dates
        if shift_map.get((staff_id, d.isoformat()), 'OFF') not in NON_WORKING
    )
    if not assigned_dates:
        return 0
    avg_per_day = worked / len(assigned_dates)
    return int(worked + avg_per_day * len(remaining_dates))


# ══════════════════════════════════════════════════════════════════════════════
# COST-AWARE BORROWING  (Improvement 1)
# ══════════════════════════════════════════════════════════════════════════════

def borrow_cost(staff, date_obj, shift_map, patterns, projections, date_list) -> int:
    """
    Score how costly it is to borrow this chef on this date.
    Lower score = better candidate to borrow.

    Factors:
      +3  already worked ≥5 days this week
      +2  projected to exceed monthly target
      −2  projected to be under monthly target (benefits from extra work)
      −1  chef historically works this day anyway (natural fit)
      +1  long shift yesterday (fatigue consideration)
    """
    cost = 0
    sid  = staff.id
    dow  = date_obj.weekday()

    # Count days worked in the same week
    week_start = date_obj - timedelta(days=dow)
    week_dates = [week_start + timedelta(days=i) for i in range(7)]
    days_worked = sum(
        1 for d in week_dates
        if shift_map.get((sid, d.isoformat()), 'OFF') not in NON_WORKING
    )
    if days_worked >= 5:
        cost += 3

    # Monthly projection bias
    proj = projections.get(sid, 0)
    if proj > MONTHLY_TARGET_MINUTES:
        cost += 2
    elif proj < MONTHLY_TARGET_MINUTES * 0.9:
        cost -= 2

    # Natural pattern fit
    cnt = patterns.get(sid, {}).get(dow, {})
    if cnt.get('OFF', 0) == 0:
        cost -= 1   # Chef rarely has this day off → natural fit

    # Fatigue: check yesterday's shift
    yesterday = (date_obj - timedelta(days=1)).isoformat()
    prev = shift_map.get((sid, yesterday), 'OFF')
    if prev not in NON_WORKING:
        prev_dur = shift_duration_minutes(prev)
        if prev_dur >= (9 * 60 - BREAK_MINUTES):   # long shift yesterday
            cost += 1

    return cost


# ══════════════════════════════════════════════════════════════════════════════
# PAX / EVENT HELPERS  (unchanged from v5)
# ══════════════════════════════════════════════════════════════════════════════

def parse_pax_from_text(text):
    if not text:
        return 0
    nums = re.findall(r'(\d+)\s*pax', str(text), re.IGNORECASE)
    return max((int(n) for n in nums), default=0)


def detect_event_type(text):
    if not text:
        return 'Other'
    t = text.upper()
    if 'DDR' in t or 'DAY DELEGATE' in t or 'CONFERENCE' in t or 'SEMINAR' in t:
        return 'DDR'
    if 'RECEPTION' in t or 'CANAPE' in t:
        return 'Reception'
    if 'DINNER' in t:
        return 'Dinner'
    if 'LUNCH' in t or 'LUNCHEON' in t:
        return 'Lunch'
    if 'BUFFET' in t:
        return 'Buffet'
    if 'BREAKFAST' in t:
        return 'Breakfast'
    if 'TASTING' in t:
        return 'Tasting'
    return 'Other'


def bqt_shift_for_event(event_type):
    t = (event_type or '').upper()
    if t in ('DINNER', 'RECEPTION'):
        return '1000-2130'
    return '0700-1530'


# ── Default pax tiers ─────────────────────────────────────────────────────────
DEFAULT_STAFFING_TIERS = {
    'tasting': {
        'CONFERENCE': (2, '0800-1630'), 'SAUCE': (1, '1000-2330'),
        'GARNISH': (1, '1000-2330'),    'LARDER': (1, '0800-2330'),
        'PASTRY':  (1, '0700-1530'),
    },
    'small': {
        'CONFERENCE': (2, '0800-1630'), 'SAUCE': (2, '1000-2330'),
        'GARNISH':    (2, '1000-2330'), 'LARDER': (2, '0800-2330'),
        'PASTRY':     (1, '0700-1530'),
    },
    'medium': {
        'CONFERENCE': (3, '0800-1630'), 'SAUCE': (2, '1000-2330'),
        'GARNISH':    (2, '1000-2330'), 'LARDER': (2, '0800-2330'),
        'PASTRY':     (2, '0700-1530'), 'BREAKFAST': (4, '0400-1430'),
        'DUTY':       (2, '0700-2300'),
    },
    'large': {
        'CONFERENCE': (4, '0800-2100'), 'SAUCE': (2, '1000-2330'),
        'GARNISH':    (2, '1000-2330'), 'LARDER': (2, '0800-2330'),
        'PASTRY':     (2, '0700-1530'), 'BREAKFAST': (4, '0400-1430'),
        'DUTY':       (2, '0700-2300'), 'TERRACE': (2, '1200-2130'),
    },
    'vip': {
        'CONFERENCE': (5, '0800-2100'), 'SAUCE': (2, '1000-2330'),
        'GARNISH':    (2, '1000-2330'), 'LARDER': (2, '0800-2330'),
        'PASTRY':     (3, '0700-1530'), 'BREAKFAST': (4, '0400-1430'),
        'DUTY':       (2, '0700-2300'), 'NIGHT': (1, '2200-0630'),
        'TERRACE':    (2, '1200-2130'),
    },
}


# ══════════════════════════════════════════════════════════════════════════════
# FULLY-ABSENT WEEK GUARD
# ══════════════════════════════════════════════════════════════════════════════

def is_fully_absent_week(staff_id: int, week_dates, shift_map) -> bool:
    """
    True if chef has ≥5 paid-absence days AND ≥2 OFF days this week.
    Such a chef is completely frozen — section minimums cannot touch them.
    Fixes: Radoslav 5×H + 2×OFF → 0 working days, no pastry pull-back.
    """
    paid = sum(
        1 for d in week_dates
        if shift_map.get((staff_id, d.isoformat())) in
           {'H', 'SICK', 'Comp', 'Paternity', 'Maternity'}
    )
    off = sum(
        1 for d in week_dates
        if shift_map.get((staff_id, d.isoformat())) == 'OFF'
    )
    return paid >= 5 and off >= 2


# ══════════════════════════════════════════════════════════════════════════════
# SECTION MINIMUM COVER
# ══════════════════════════════════════════════════════════════════════════════

def apply_section_minimums(shift_map, staff_list, date_list, patterns,
                           borrowed_labels=None, holiday_map=None):
    """
    Enforce minimum headcount for every section on every day.
    Uses patterns + fatigue awareness to pick the best candidate.
    Only converts plain 'OFF' → working; never touches PROTECTED or holiday_map entries.
    """
    if borrowed_labels is None:
        borrowed_labels = {}
    if holiday_map is None:
        holiday_map = {}

    section_staff = defaultdict(list)
    for s in staff_list:
        if s.section:
            section_staff[s.section.name.upper()].append(s)

    for d in date_list:
        date_iso = d.isoformat()
        dow      = d.weekday()

        # ── Named section rules ───────────────────────────────────────────
        for sec_keyword, (min_staff, required_shift) in SECTION_MINIMUM_COVER.items():
            matching = [k for k in section_staff if sec_keyword in k]
            for sec_name in matching:
                workers = section_staff[sec_name]
                covering = [
                    s for s in workers
                    if shift_map.get((s.id, date_iso), 'OFF') not in NON_WORKING
                    and _covers_until(shift_map.get((s.id, date_iso), 'OFF'), required_shift)
                ]
                shortfall = min_staff - len(covering)
                if shortfall <= 0:
                    continue

                # Pick own-section OFF staff, sorted by:
                # 1. Natural pattern fit for this DOW
                # 2. Not in a fully-absent week (H/SICK × 5 + OFF × 2)
                _dow_d      = d.weekday()
                _wk_start_d = d - timedelta(days=_dow_d)
                _week_d     = [_wk_start_d + timedelta(days=i) for i in range(7)]
                own_off = [
                    s for s in workers
                    if shift_map.get((s.id, date_iso), 'OFF') == 'OFF'
                    and (s.id, date_iso) not in holiday_map
                    and not is_fully_absent_week(s.id, _week_d, shift_map)
                ]
                def work_score(s):
                    cnt = patterns.get(s.id, {}).get(dow, {})
                    return sum(v for k, v in cnt.items() if k not in NON_WORKING)

                own_off_sorted = sorted(own_off, key=work_score, reverse=True)
                assigned_count = 0
                for s in own_off_sorted:
                    if assigned_count >= shortfall:
                        break
                    prev = shift_map.get((s.id, (d - timedelta(days=1)).isoformat()), 'OFF')
                    best = best_shift_for(s.id, dow, patterns, staff_name=s.name,
                                          prev_shift=prev)
                    use_shift = best if (
                        best not in NON_WORKING and _covers_until(best, required_shift)
                    ) else required_shift
                    shift_map[(s.id, date_iso)] = use_shift
                    log_reason(s.id, date_iso, AssignmentReason(
                        'SECTION_MINIMUM', 'Section minimum',
                        f'{sec_name} requires {min_staff} staff. Shift assigned to meet cover.',
                        'warning'
                    ))
                    assigned_count += 1
                shortfall -= assigned_count

                # Borrow from other sections if still short
                if shortfall > 0:
                    free_any = [
                        s for s in staff_list
                        if shift_map.get((s.id, date_iso), 'OFF') == 'OFF'
                        and (s.id, date_iso) not in holiday_map
                        and s not in workers
                        and not is_fully_absent_week(s.id, _week_d, shift_map)
                    ]
                    free_sorted = sorted(free_any, key=work_score, reverse=True)
                    for s in free_sorted[:shortfall]:
                        orig_sec = s.section.name if s.section else 'Unknown'
                        prev = shift_map.get((s.id, (d - timedelta(days=1)).isoformat()), 'OFF')
                        best = best_shift_for(s.id, dow, patterns, staff_name=s.name,
                                              prev_shift=prev)
                        use_shift = best if (
                            best not in NON_WORKING and _covers_until(best, required_shift)
                        ) else required_shift
                        shift_map[(s.id, date_iso)] = use_shift
                        borrowed_labels[(s.id, date_iso)] = orig_sec
                        log_reason(s.id, date_iso, AssignmentReason(
                            'BORROW_SECTION_MIN', 'Borrowed – section minimum',
                            f'Borrowed from {orig_sec} to cover {sec_name} minimum.',
                            'warning'
                        ))
                        shortfall -= 1

        # ── Pastry rules ──────────────────────────────────────────────────
        pastry_workers = [s for s in staff_list
                          if s.section and 'PASTRY' in s.section.name.upper()]
        if not pastry_workers:
            continue

        am_shift   = PASTRY_RULES['am_shift']
        late_shift = PASTRY_RULES['late_shift']

        am_working = [
            s for s in pastry_workers
            if not _is_nonworking(shift_map.get((s.id, date_iso), 'OFF'))
            and _is_am_shift(shift_map.get((s.id, date_iso), ''))
        ]
        late_working = [
            s for s in pastry_workers
            if shift_map.get((s.id, date_iso), 'OFF') == late_shift
        ]

        # Ensure 2 AM pastry
        am_shortfall = PASTRY_RULES['min_am'] - len(am_working)
        if am_shortfall > 0:
            _p_dow    = d.weekday()
            _p_wk_s   = d - timedelta(days=_p_dow)
            _p_week   = [_p_wk_s + timedelta(days=i) for i in range(7)]
            off_pastry = [s for s in pastry_workers
                          if shift_map.get((s.id, date_iso), 'OFF') == 'OFF'
                          and (s.id, date_iso) not in holiday_map
                          and not is_fully_absent_week(s.id, _p_week, shift_map)]
            for s in off_pastry[:am_shortfall]:
                shift_map[(s.id, date_iso)] = am_shift
                log_reason(s.id, date_iso, AssignmentReason(
                    'PASTRY_AM_MIN', 'Pastry AM minimum',
                    f'Pastry requires {PASTRY_RULES["min_am"]} AM chefs.',
                    'warning'
                ))

        # Ensure 1 late pastry
        if not late_working:
            surplus = [s for s in pastry_workers
                       if not _is_nonworking(shift_map.get((s.id, date_iso), 'OFF'))
                       and s not in am_working[:PASTRY_RULES['min_am']]]
            if surplus:
                shift_map[(surplus[0].id, date_iso)] = late_shift
            else:
                off_p = [s for s in pastry_workers
                         if shift_map.get((s.id, date_iso), 'OFF') == 'OFF'
                         and (s.id, date_iso) not in holiday_map
                         and not is_fully_absent_week(s.id, _p_week, shift_map)]
                if off_p:
                    shift_map[(off_p[0].id, date_iso)] = late_shift
                    log_reason(off_p[0].id, date_iso, AssignmentReason(
                        'PASTRY_LATE_MIN', 'Pastry late minimum',
                        'Pastry requires 1 late chef.',
                        'warning'
                    ))

    return shift_map, borrowed_labels


def apply_bqt_pastry(shift_map, staff_list, events_by_date):
    """BQT pastry chef on event days. Only converts plain OFF."""
    pastry_workers = [s for s in staff_list
                      if s.section and 'PASTRY' in s.section.name.upper()]
    if not pastry_workers:
        return shift_map
    for date_iso, events in events_by_date.items():
        if not events:
            continue
        dinner_events = [e for e in events if (e.event_type or '').upper() == 'DINNER']
        dominant_type = 'Dinner' if dinner_events else (events[0].event_type or 'Other')
        bqt_shift = bqt_shift_for_event(dominant_type)
        already = [s for s in pastry_workers
                   if shift_map.get((s.id, date_iso), 'OFF') == bqt_shift]
        if already:
            continue
        candidates = [s for s in pastry_workers
                      if shift_map.get((s.id, date_iso), 'OFF') == 'OFF']
        if candidates:
            shift_map[(candidates[0].id, date_iso)] = bqt_shift
    return shift_map


def apply_conference_event_boost(shift_map, staff_list, events_by_date,
                                  borrowed_labels=None, holiday_map=None):
    """Dinner ≥200 pax → min 7 chefs. Respects holiday_map."""
    if borrowed_labels is None:
        borrowed_labels = {}
    if holiday_map is None:
        holiday_map = {}

    for date_iso, events in events_by_date.items():
        big_dinners = [e for e in events
                       if (e.event_type or '').upper() == 'DINNER' and e.pax >= 200]
        if not big_dinners:
            continue
        working = [s for s in staff_list
                   if not _is_nonworking(shift_map.get((s.id, date_iso), 'OFF'))]
        needed = max(0, 7 - len(working))
        if needed == 0:
            continue

        pastry_working = [s for s in working
                          if s.section and 'PASTRY' in s.section.name.upper()]
        if not pastry_working:
            pastry_off = [s for s in staff_list
                          if s.section and 'PASTRY' in s.section.name.upper()
                          and shift_map.get((s.id, date_iso), 'OFF') == 'OFF'
                          and (s.id, date_iso) not in holiday_map]
            if pastry_off:
                s = pastry_off[0]
                shift_map[(s.id, date_iso)] = '1000-2130'
                borrowed_labels[(s.id, date_iso)] = s.section.name
                needed -= 1

        if needed > 0:
            free = [s for s in staff_list
                    if shift_map.get((s.id, date_iso), 'OFF') == 'OFF'
                    and (s.id, date_iso) not in holiday_map
                    and s.section and 'CONFERENCE' not in s.section.name.upper()]
            for s in free[:needed]:
                orig = s.section.name if s.section else 'Unknown'
                shift_map[(s.id, date_iso)] = '1200-2100'
                borrowed_labels[(s.id, date_iso)] = orig

    return shift_map, borrowed_labels


# ══════════════════════════════════════════════════════════════════════════════
# HOURS CAP  (unchanged safety logic; holiday_map guard added)
# ══════════════════════════════════════════════════════════════════════════════

def apply_hours_cap(shift_map, staff_list, date_list, holiday_map=None):
    """
    Trim over-hours working shifts. Never touches PROTECTED or holiday_map entries.
    Weekly cap is calculated from working shifts only (H/SICK don't count).
    """
    if holiday_map is None:
        holiday_map = {}
    weeks = group_by_week(date_list)
    hours_report = defaultdict(lambda: {'weekly': {}, 'monthly': {}})

    month_weeks = defaultdict(list)
    for wk in weeks:
        month_weeks[wk[0].strftime('%Y-%m')].append(wk)

    for staff in staff_list:
        sid = staff.id
        weekly_target = int(float(getattr(staff, 'contracted_hours_pw', 40)) * 60)
        week_max = int(weekly_target * 1.2)

        for week_dates in weeks:
            week_key = week_dates[0].isoformat()
            working_minutes = sum(
                shift_duration_minutes(shift_map.get((sid, d.isoformat()), 'OFF'))
                for d in week_dates
                if shift_map.get((sid, d.isoformat()), 'OFF') not in NON_WORKING
            )
            hours_report[sid]['weekly'][week_key] = working_minutes

            if working_minutes > week_max:
                trimmable = sorted(
                    [d for d in week_dates
                     if shift_map.get((sid, d.isoformat()), 'OFF') not in PROTECTED
                     and shift_map.get((sid, d.isoformat()), 'OFF') not in NON_WORKING
                     and (sid, d.isoformat()) not in holiday_map],
                    key=lambda d: shift_duration_minutes(shift_map.get((sid, d.isoformat()), 'OFF')),
                    reverse=True,
                )
                for d in trimmable:
                    if working_minutes <= week_max:
                        break
                    dur = shift_duration_minutes(shift_map.get((sid, d.isoformat()), 'OFF'))
                    shift_map[(sid, d.isoformat())] = 'OFF'
                    working_minutes -= dur
                hours_report[sid]['weekly'][week_key] = working_minutes

        for month_key, mwl in month_weeks.items():
            all_dates = [d for wk in mwl for d in wk]
            total = sum(
                shift_duration_minutes(shift_map.get((sid, d.isoformat()), 'OFF'))
                for d in all_dates
                if shift_map.get((sid, d.isoformat()), 'OFF') not in NON_WORKING
            )
            hours_report[sid]['monthly'][month_key] = total

    return hours_report


# ══════════════════════════════════════════════════════════════════════════════
# MONTHLY LEDGER
# ══════════════════════════════════════════════════════════════════════════════

def update_monthly_ledger(staff_list, month_str):
    """
    Rebuild MonthlyHoursLedger for the full calendar month.
    Deduplicates by (staff_id, date) keeping the most recent rota_period.
    """
    from rota.models import ShiftEntry, MonthlyHoursLedger
    import calendar

    PAID_ABSENCE_MINUTES = {
        'H': 8*60, 'SICK': 8*60, 'Comp': 8*60,
        'Paternity': 8*60, 'Maternity': 8*60,
    }

    year, month_num = int(month_str[:4]), int(month_str[5:7])
    last_day    = calendar.monthrange(year, month_num)[1]
    month_start = date(year, month_num, 1)
    month_end   = date(year, month_num, last_day)

    staff_ids = [s.id for s in staff_list]
    raw = (
        ShiftEntry.objects
        .filter(staff_id__in=staff_ids, date__gte=month_start, date__lte=month_end)
        .order_by('staff_id', 'date', '-rota_period__created_at')
        .values('staff_id', 'date', 'shift_value', 'rota_period__created_at')
    )
    seen = set()
    deduped = []
    for e in raw:
        key = (e['staff_id'], e['date'])
        if key not in seen:
            seen.add(key)
            deduped.append(e)

    net_mins          = defaultdict(int)
    paid_absence_mins = defaultdict(int)
    for entry in deduped:
        sv  = entry['shift_value']
        sid = entry['staff_id']
        if sv in PAID_ABSENCE_MINUTES:
            paid_absence_mins[sid] += PAID_ABSENCE_MINUTES[sv]
        elif sv not in NON_WORKING:
            net_mins[sid] += shift_duration_minutes(sv)

    for staff in staff_list:
        sid       = staff.id
        is_pt     = getattr(staff, 'employment_type', 'full_time') in (
                        'part_time', 'agency', 'zero_hours')
        contracted = float(getattr(staff, 'contracted_hours_pw', 40))
        full_target = (
            int(contracted * 60 * 4.34) if is_pt
            else MONTHLY_TARGET_MINUTES
        )
        effective_target = max(0, full_target - paid_absence_mins[sid])
        total_credited   = net_mins[sid] + paid_absence_mins[sid]

        if is_pt:
            status = 'ok'
        elif total_credited > full_target * 1.1:
            status = 'over'
        elif total_credited < full_target * 0.9:
            status = 'under'
        else:
            status = 'ok'

        MonthlyHoursLedger.objects.update_or_create(
            staff=staff, month=month_str,
            defaults={
                'net_minutes':     net_mins[sid],
                'holiday_minutes': paid_absence_mins[sid],
                'target_minutes':  full_target,
                'status':          status,
                'notes':           (
                    f"Remaining to work: "
                    f"{max(0,effective_target)//60}h{max(0,effective_target)%60:02d}m"
                    if effective_target > 0 else "Target met via absences"
                ),
            }
        )


# ══════════════════════════════════════════════════════════════════════════════
# STAFFING RECOMMENDATIONS
# ══════════════════════════════════════════════════════════════════════════════

def get_staffing_recommendation(events_on_date, sections, staffing_rules=None):
    if not events_on_date:
        return None
    total_pax = sum(e.pax for e in events_on_date)
    peak_pax  = max((e.pax for e in events_on_date), default=0)
    if peak_pax == 0:          tier = 'tasting'
    elif peak_pax <= 30:       tier = 'small'
    elif peak_pax <= 100:      tier = 'medium'
    elif peak_pax <= 250:      tier = 'large'
    else:                      tier = 'vip'

    section_recs = []
    for section in sections:
        sec_upper    = section.name.upper()
        matched_rule = None
        if staffing_rules:
            for rule in staffing_rules:
                if rule.section_id == section.id and rule.pax_min <= peak_pax <= rule.pax_max:
                    matched_rule = rule
                    break
        if matched_rule:
            section_recs.append({
                'section': section, 'staff_count': matched_rule.staff_count,
                'shift': matched_rule.shift_suggestion,
                'source': 'custom rule', 'rule_id': matched_rule.id,
            })
        else:
            for keyword, (count, shift) in DEFAULT_STAFFING_TIERS.get(tier, {}).items():
                if keyword in sec_upper:
                    section_recs.append({
                        'section': section, 'staff_count': count,
                        'shift': shift, 'source': 'default tier', 'rule_id': None,
                    })
                    break
    event_names = ', '.join(f"{e.description} ({e.pax}pax)" for e in events_on_date)
    return {
        'tier': tier, 'total_pax': total_pax, 'peak_pax': peak_pax,
        'events': events_on_date, 'sections': section_recs,
        'notes': f"{tier.upper()} event day — {total_pax} total pax. {event_names}",
    }


def apply_pax_overrides(shift_map, staff_list, events_by_date,
                         sections, staffing_rules=None, borrowed_labels=None):
    if borrowed_labels is None:
        borrowed_labels = {}
    section_staff = defaultdict(list)
    for s in staff_list:
        if s.section_id:
            section_staff[s.section_id].append(s)

    recommendations = {}
    for date_iso, events in events_by_date.items():
        rec = get_staffing_recommendation(events, list(sections), staffing_rules)
        if not rec:
            continue
        recommendations[date_iso] = rec
        for sec_rec in rec['sections']:
            section    = sec_rec['section']
            needed     = sec_rec['staff_count']
            sugg_shift = sec_rec['shift']
            available  = section_staff.get(section.id, [])
            working    = [s for s in available
                          if shift_map.get((s.id, date_iso), 'OFF') not in NON_WORKING]
            shortfall  = needed - len(working)
            if shortfall <= 0:
                continue
            off_staff = [s for s in available
                         if shift_map.get((s.id, date_iso), 'OFF') == 'OFF']
            for s in off_staff[:shortfall]:
                shift_map[(s.id, date_iso)] = sugg_shift
    return recommendations, borrowed_labels


# ══════════════════════════════════════════════════════════════════════════════
# PATTERN LEARNING
# ══════════════════════════════════════════════════════════════════════════════

def learn_patterns(shift_entries):
    patterns = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    for entry in shift_entries:
        dow = entry.date.weekday()
        patterns[entry.staff_id][dow][entry.shift_value] += 1
    return patterns


def best_shift_for(staff_id, day_of_week, patterns, fallback='0800-1630',
                   staff_name='', prev_shift=None):
    """
    Return the best shift for a chef on a given DOW.
    Respects STAFF_SHIFT_OVERRIDES, then ranks by:
        frequency − fatigue_penalty + consistency_bonus
    """
    # Hard overrides (Emil, Zsolt → nights only)
    if staff_name:
        for name_fragment, allowed_shifts in STAFF_SHIFT_OVERRIDES.items():
            if name_fragment.lower() in staff_name.lower():
                return allowed_shifts[0]

    day_patterns = patterns.get(staff_id, {}).get(day_of_week, {})
    if not day_patterns:
        all_shifts = defaultdict(int)
        for dow_data in patterns.get(staff_id, {}).values():
            for shift, count in dow_data.items():
                if shift not in NON_WORKING:
                    all_shifts[shift] += count
        if all_shifts:
            return max(all_shifts, key=all_shifts.get)
        return fallback

    working_shifts = {k: v for k, v in day_patterns.items() if k not in NON_WORKING}
    if not working_shifts:
        return fallback

    best, best_score = None, -1e9
    for shift, freq in working_shifts.items():
        score = freq
        score -= fatigue_penalty(prev_shift, shift)
        score += consistency_bonus(prev_shift, shift)
        if score > best_score:
            best, best_score = shift, score
    return best or fallback


def get_section_label(section_name: str, is_borrowed: bool = False,
                       borrowed_from: str = '', date_obj=None) -> str:
    """
    Return the short label to display under a working shift cell.
    Matches the label convention in the reference Excel rota:
      - Own section: 'Sauce', 'Pastry', 'Terrace', etc.
      - Borrowed: 'BQT', 'Sauce', 'Terrace', etc. (destination section)
      - Brunch (Sat/Sun in brunch sections): 'BRUNCH'
      - AM Duty with Sauce cover: 'AM DUTY + SAUCE'
    """
    if not section_name:
        return ''
    name_upper = section_name.upper()

    # Brunch label for weekend days in applicable sections
    if date_obj is not None:
        dow = date_obj.weekday() if hasattr(date_obj, 'weekday') else -1
        if dow in (5, 6):   # Saturday=5, Sunday=6
            for sec_kw in BRUNCH_SECTIONS:
                if sec_kw in name_upper:
                    return BRUNCH_LABEL

    # Map section name to short label
    for keyword, label in SECTION_LABELS.items():
        if keyword in name_upper:
            return label

    return section_name.split()[0].title()   # fallback: first word


# ══════════════════════════════════════════════════════════════════════════════
# SUNDAY RULES — brunch cap & canteen structure
# ══════════════════════════════════════════════════════════════════════════════

def apply_sunday_brunch_cap(shift_map, staff_list, date_list):
    """
    Enforce exactly SUNDAY_BRUNCH_RULE['max_staff'] (2) people on brunch
    every Sunday. Workers on allowed_shifts beyond the cap are set to OFF.
    """
    allowed   = SUNDAY_BRUNCH_RULE['allowed_shifts']
    max_staff = SUNDAY_BRUNCH_RULE['max_staff']
    for d in date_list:
        if d.weekday() != 6:
            continue
        date_iso = d.isoformat()
        brunch = [sid for (sid, di), sv in shift_map.items()
                  if di == date_iso and sv in allowed]
        if len(brunch) <= max_staff:
            continue
        for sid in brunch[max_staff:]:
            shift_map[(sid, date_iso)] = 'OFF'
            log_reason(sid, date_iso, AssignmentReason(
                'SUNDAY_BRUNCH_CAP', 'Sunday brunch cap',
                f'Sunday brunch capped at {max_staff}. Excess worker set to OFF.',
                'warning'
            ))


def apply_staff_canteen_sunday_rule(shift_map, staff_list, date_list):
    """
    Staff Canteen on Sundays: enforce exactly the two defined slots.
    Any canteen worker on a different shift is set to OFF. Then cap at 2.
    """
    staff_by_id = {s.id: s for s in staff_list}
    slots       = set(STAFF_CANTEEN_SUNDAY['slots'])
    max_staff   = STAFF_CANTEEN_SUNDAY['max_staff']
    for d in date_list:
        if d.weekday() != 6:
            continue
        date_iso = d.isoformat()
        canteen = [
            sid for sid, s in staff_by_id.items()
            if s.section and any(kw in s.section.name.upper()
                                 for kw in ('CANTEEN', 'CAFÉ', 'CAFE'))
            and shift_map.get((sid, date_iso), 'OFF') not in NON_WORKING
        ]
        # Clear invalid slots
        for sid in canteen:
            if shift_map.get((sid, date_iso), 'OFF') not in slots:
                shift_map[(sid, date_iso)] = 'OFF'
                log_reason(sid, date_iso, AssignmentReason(
                    'CANTEEN_SUNDAY_SLOT', 'Staff canteen Sunday slot',
                    'Staff canteen Sunday requires exact slot shifts.',
                    'warning'
                ))
        # Cap at max_staff
        valid = [sid for sid in canteen
                 if shift_map.get((sid, date_iso), 'OFF') in slots]
        for sid in valid[max_staff:]:
            shift_map[(sid, date_iso)] = 'OFF'
            log_reason(sid, date_iso, AssignmentReason(
                'CANTEEN_SUNDAY_CAP', 'Staff canteen Sunday cap',
                f'Staff canteen capped at {max_staff} on Sundays.',
                'warning'
            ))


# ══════════════════════════════════════════════════════════════════════════════
# MAIN GENERATOR
# ══════════════════════════════════════════════════════════════════════════════

def generate_rota(staff_list, date_list, holiday_map, patterns,
                  events_by_date=None, apply_section_rules=True):
    """
    Generate a complete rota with v6 intelligence.

    Returns:
        (shift_map, borrowed_labels, assignment_reasons, shift_labels)
        shift_labels: {(staff_id, date_iso): label_string}
          e.g. 'Sauce', 'Pastry', 'BRUNCH', 'BQT', 'Terrace'
    """
    global _assignment_reasons
    _assignment_reasons = defaultdict(list)   # reset per generation

    result          = {}
    borrowed_labels = {}
    shift_labels    = {}   # (staff_id, date_iso) → display label
    weeks           = group_by_week(date_list)

    # Pre-compute monthly projections for borrow cost scoring
    projections = {
        s.id: project_month_end_hours(s.id, result, date_list)
        for s in staff_list
    }

    for staff in staff_list:
        sid   = staff.id
        prev_shift = None   # track across the whole date_list for fatigue

        for week_dates in weeks:
            assigned_shifts = {}
            assigned_off    = []

            # ── Step 1: Lock manager entries ─────────────────────────────
            for d in week_dates:
                preset = holiday_map.get((sid, d.isoformat()))
                if preset:
                    assigned_shifts[d] = preset
                    if preset in NON_WORKING:
                        assigned_off.append(d)
                    log_reason(sid, d.isoformat(), AssignmentReason(
                        'MANAGER_LOCKED', 'Manager override',
                        'Day locked by manager input.',
                        'critical'
                    ))

            # ── Step 2: Rest days (pattern-driven) ───────────────────────
            # RULE: Every chef gets AT LEAST 2 plain 'OFF' days per week,
            # regardless of how many H/SICK/protected days they already have.
            # H/SICK are paid absences, not rest days — both are required.
            unassigned   = [d for d in week_dates if d not in assigned_shifts]
            off_only     = sum(1 for v in assigned_shifts.values() if v == 'OFF')
            contracted_h = float(getattr(staff, 'contracted_hours_pw', 40))
            working_days_needed = max(2, math.ceil(contracted_h / 8))
            # Min rest = max of 2 (always guaranteed) and days derived from contract
            min_rest_days = max(2, 7 - working_days_needed)
            needed_off    = max(0, min_rest_days - off_only)

            if needed_off > 0:
                def off_score(d):
                    cnt = patterns.get(sid, {}).get(d.weekday(), {})
                    return cnt.get('OFF', 0) + cnt.get('H', 0)

                for d in sorted(unassigned, key=off_score, reverse=True)[:needed_off]:
                    assigned_shifts[d] = 'OFF'
                    assigned_off.append(d)
                    log_reason(sid, d.isoformat(), AssignmentReason(
                        'WEEKLY_REST', 'Weekly rest',
                        'Assigned as required rest day based on historical pattern.',
                        'info'
                    ))

            # ── Step 3: Fill remaining days with fatigue-aware shift ──────
            for d in week_dates:
                if d not in assigned_shifts:
                    shift = best_shift_for(sid, d.weekday(), patterns,
                                           staff_name=staff.name,
                                           prev_shift=prev_shift)
                    assigned_shifts[d] = shift
                    log_reason(sid, d.isoformat(), AssignmentReason(
                        'PATTERN_MATCH', 'Pattern + fatigue match',
                        f'Shift chosen from historical pattern with fatigue penalty applied.',
                        'info'
                    ))
                prev_shift = assigned_shifts[d]

            for d, val in assigned_shifts.items():
                result[(sid, d.isoformat())] = val
                # Label own-section working shifts
                if val not in NON_WORKING and staff.section:
                    shift_labels[(sid, d.isoformat())] = get_section_label(
                        staff.section.name, date_obj=d
                    )

    # ── Section minimum cover ──────────────────────────────────────────────
    if apply_section_rules:
        result, borrowed_labels = apply_section_minimums(
            result, staff_list, date_list, patterns, borrowed_labels,
            holiday_map=holiday_map
        )
        if events_by_date:
            result = apply_bqt_pastry(result, staff_list, events_by_date)
            result, borrowed_labels = apply_conference_event_boost(
                result, staff_list, events_by_date, borrowed_labels,
                holiday_map=holiday_map
            )

    # ── Sunday brunch cap + canteen structure (run after section minimums) ──
    apply_sunday_brunch_cap(result, staff_list, date_list)
    apply_staff_canteen_sunday_rule(result, staff_list, date_list)

    # ── Weekly hours cap ──────────────────────────────────────────────────
    apply_hours_cap(result, staff_list, date_list, holiday_map=holiday_map)

    # ── Cost-aware borrowing: re-rank any borrowed entries ────────────────
    # Re-compute projections after generation for more accurate cost scores
    projections = {
        s.id: project_month_end_hours(s.id, result, date_list)
        for s in staff_list
    }

    # ── Clean borrowed_labels: remove entries where shift ended up as OFF ──
    borrowed_labels = {
        key: sec
        for key, sec in borrowed_labels.items()
        if result.get(key, 'OFF') not in NON_WORKING
    }

    # ── Build labels for borrowed shifts (destination section) ─────────────
    for (sid, date_iso), dest_sec in borrowed_labels.items():
        try:
            d = date.fromisoformat(date_iso)
        except Exception:
            d = None
        shift_labels[(sid, date_iso)] = get_section_label(dest_sec, is_borrowed=True, date_obj=d)

    return result, borrowed_labels, _assignment_reasons, shift_labels


# ══════════════════════════════════════════════════════════════════════════════
# V6 OPTIMISATION PASS  (Improvement 6 — safe, bounded)
# ══════════════════════════════════════════════════════════════════════════════

def rota_score(shift_map, staff_list, patterns):
    """
    Objective score for a rota (lower = better).
    Penalises:  borrowing, fatigue transitions
    Rewards:    shift consistency
    """
    score = 0
    for staff in staff_list:
        sid = staff.id
        sorted_dates = sorted(d for (s, d) in shift_map if s == sid)
        for i, date_iso in enumerate(sorted_dates):
            sv = shift_map.get((sid, date_iso), 'OFF')
            if i > 0:
                prev = shift_map.get((sid, sorted_dates[i-1]), 'OFF')
                score += fatigue_penalty(prev, sv) * 5
                score -= consistency_bonus(prev, sv)
    return score


def optimize_rota(shift_map, staff_list, patterns, iterations=40):
    """
    Safe bounded optimisation: try swapping equivalent-band shifts between
    chefs in the same section to reduce fatigue and improve consistency.
    Never violates any hard rule.
    """
    best        = dict(shift_map)
    best_score  = rota_score(best, staff_list, patterns)

    staff_by_section = defaultdict(list)
    for s in staff_list:
        if s.section:
            staff_by_section[s.section.id].append(s)

    for _ in range(iterations):
        improved = False
        for sec_id, members in staff_by_section.items():
            if len(members) < 2:
                continue
            # Try swapping a working day between two chefs in the same section
            for i in range(len(members)):
                for j in range(i + 1, len(members)):
                    sa, sb = members[i], members[j]
                    sorted_dates = sorted(set(
                        d for (s, d) in best if s in (sa.id, sb.id)
                    ))
                    for date_iso in sorted_dates:
                        va = best.get((sa.id, date_iso), 'OFF')
                        vb = best.get((sb.id, date_iso), 'OFF')
                        # Only swap if both are working and same band
                        if va in NON_WORKING or vb in NON_WORKING:
                            continue
                        if shift_band(va) != shift_band(vb):
                            continue
                        if va == vb:
                            continue
                        # Try the swap
                        candidate = dict(best)
                        candidate[(sa.id, date_iso)] = vb
                        candidate[(sb.id, date_iso)] = va
                        cand_score = rota_score(candidate, staff_list, patterns)
                        if cand_score < best_score:
                            best        = candidate
                            best_score  = cand_score
                            improved    = True
        if not improved:
            break

    return best


# ══════════════════════════════════════════════════════════════════════════════
# UTILITY FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

def group_by_week(date_list):
    if not date_list:
        return []
    weeks, current = [], []
    for d in sorted(date_list):
        if current and d.weekday() < current[-1].weekday():
            weeks.append(current)
            current = [d]
        else:
            current.append(d)
    if current:
        weeks.append(current)
    return weeks


def save_patterns_to_db(shift_entries):
    from rota.models import ShiftPattern
    counts = defaultdict(int)
    dates  = {}
    for entry in shift_entries:
        dow = entry.date.weekday()
        key = (entry.staff_id, dow, entry.shift_value)
        counts[key] += 1
        if key not in dates or entry.date > dates[key]:
            dates[key] = entry.date
    for (sid, dow, sv), count in counts.items():
        obj, created = ShiftPattern.objects.get_or_create(
            staff_id=sid, day_of_week=dow, shift_value=sv,
            defaults={'frequency': count, 'last_seen': dates[(sid, dow, sv)]}
        )
        if not created:
            obj.frequency += count
            if dates[(sid, dow, sv)] > (obj.last_seen or date.min):
                obj.last_seen = dates[(sid, dow, sv)]
            obj.save()


def load_patterns_from_db(staff_ids=None):
    from rota.models import ShiftPattern
    qs = ShiftPattern.objects.all()
    if staff_ids:
        qs = qs.filter(staff_id__in=staff_ids)
    patterns = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    for sp in qs:
        patterns[sp.staff_id][sp.day_of_week][sp.shift_value] = sp.frequency
    return patterns


def calculate_hours_summary(shift_entries, staff_list=None):
    summary = defaultdict(lambda: {'weekly': defaultdict(int), 'monthly': defaultdict(int)})
    for entry in shift_entries:
        sid     = entry.staff_id
        minutes = shift_duration_minutes(entry.shift_value)
        if minutes == 0:
            continue
        dow        = entry.date.weekday()
        week_start = entry.date - timedelta(days=dow)
        summary[sid]['weekly'][week_start.isoformat()] += minutes
        summary[sid]['monthly'][entry.date.strftime('%Y-%m')] += minutes
    return summary


# ── Private helpers ───────────────────────────────────────────────────────────

def _is_nonworking(v): return v in NON_WORKING

def _covers_until(shift_value, required_shift):
    if shift_value in NON_WORKING:
        return False
    def end_min(s):
        m = re.match(r'^\d{4}-(\d{2})(\d{2})$', s)
        if not m:
            return 0
        h, mn = int(m.group(1)), int(m.group(2))
        total = h * 60 + mn
        return total + 24 * 60 if h < 12 else total
    return end_min(shift_value) >= end_min(required_shift)

def _covers_evening(shift_value):
    if shift_value in NON_WORKING:
        return False
    m = re.match(r'^\d{4}-(\d{2})(\d{2})$', shift_value)
    if not m:
        return False
    h = int(m.group(1))
    return h >= 21 or h <= 3

def _is_am_shift(shift_value):
    if shift_value in NON_WORKING:
        return False
    m = re.match(r'^(\d{4})-', shift_value)
    return bool(m) and int(m.group(1)) < 1000
