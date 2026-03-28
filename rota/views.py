import json
import os
import re
from datetime import date, timedelta
from collections import defaultdict

from django.shortcuts import render, redirect, get_object_or_404
from django.http import HttpResponse, JsonResponse
from django.contrib import messages
from django.db import transaction
from django.views.decorators.http import require_POST
from django.views.decorators.csrf import ensure_csrf_cookie

from .models import Section, Staff, RotaPeriod, ShiftEntry, ShiftPattern, Event, StaffingRule, MonthlyHoursLedger
from .excel_parser import parse_workbook
from .excel_export import export_rota_to_xlsx
from .ml.algorithm import (
    learn_patterns, generate_rota, save_patterns_to_db, load_patterns_from_db,
    parse_pax_from_text, detect_event_type, get_staffing_recommendation,
    apply_pax_overrides, calculate_hours_summary, shift_color_class,
    shift_duration_minutes, group_by_week, SECTION_COLORS, update_monthly_ledger,
    optimize_rota, get_assignment_reasons,
)

# ─── Dashboard ────────────────────────────────────────────────────────────────

def dashboard(request):
    periods = RotaPeriod.objects.order_by('-start_date')[:10]
    return render(request, 'rota/dashboard.html', {
        'periods': periods,
        'staff_count': Staff.objects.filter(is_active=True).count(),
        'section_count': Section.objects.count(),
        'pattern_count': ShiftPattern.objects.count(),
    })


# ─── Import ────────────────────────────────────────────────────────────────────

def import_rota(request):
    if request.method == 'POST':
        uploaded = request.FILES.get('rota_file')
        if not uploaded:
            messages.error(request, 'Please select a file.')
            return redirect('import_rota')

        import tempfile
        with tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False) as tmp:
            for chunk in uploaded.chunks():
                tmp.write(chunk)
            tmp_path = tmp.name

        try:
            parsed_periods = parse_workbook(tmp_path)
        except Exception as e:
            messages.error(request, f'Failed to parse file: {e}')
            return redirect('import_rota')
        finally:
            os.unlink(tmp_path)

        imported_count = 0
        with transaction.atomic():
            for period_data in parsed_periods:
                dates = [d for d in period_data['dates'] if d]
                if not dates:
                    continue

                rota_period, _ = RotaPeriod.objects.get_or_create(
                    label=period_data['label'],
                    defaults={
                        'start_date': min(dates),
                        'end_date': max(dates),
                        'highlights': period_data['highlights'],
                    }
                )

                section_order = 0
                new_entries = []
                for row in period_data['sections_data']:
                    section_name = row['section']
                    section, _ = Section.objects.get_or_create(
                        name=section_name, defaults={'order': section_order}
                    )
                    section_order += 1

                    staff, _ = Staff.objects.get_or_create(
                        name=row['name'],
                        defaults={'role': row['role'], 'section': section, 'is_active': True}
                    )
                    if staff.section is None:
                        staff.section = section
                        staff.save()

                    for date_iso, shift_val in row['shifts'].items():
                        entry, created = ShiftEntry.objects.get_or_create(
                            rota_period=rota_period, staff=staff,
                            date=date.fromisoformat(date_iso),
                            defaults={'shift_value': shift_val}
                        )
                        if created:
                            new_entries.append(entry)

                imported_count += len(new_entries)
                save_patterns_to_db(new_entries)
                _parse_highlights_to_events(rota_period, period_data['highlights'], dates)

        messages.success(request, f'Imported {imported_count} shift entries across {len(parsed_periods)} period(s).')
        return redirect('dashboard')

    return render(request, 'rota/import.html')


def _parse_highlights_to_events(rota_period, highlights_text, dates):
    if not highlights_text:
        return
    parts = re.split(r'\s*\|\s*', highlights_text)
    for part in parts:
        part = part.strip()
        if not part:
            continue
        pax = parse_pax_from_text(part)
        etype = detect_event_type(part)
        Event.objects.get_or_create(
            rota_period=rota_period, description=part,
            defaults={
                'date': min(dates) if dates else rota_period.start_date,
                'pax': pax, 'event_type': etype,
            }
        )


# ─── View Rota ─────────────────────────────────────────────────────────────────

@ensure_csrf_cookie
def view_rota(request, period_id):
    period = get_object_or_404(RotaPeriod, pk=period_id)
    entries = list(ShiftEntry.objects.filter(rota_period=period)
                   .select_related('staff', 'staff__section'))
    events = list(Event.objects.filter(rota_period=period).order_by('date', '-pax'))
    dates = sorted(set(e.date for e in entries))
    sections = list(Section.objects.order_by('order'))

    rota_data = []
    for section in sections:
        staff_in_section = [s for s in Staff.objects.filter(section=section, is_active=True)]
        staff_rows = []
        for s in staff_in_section:
            staff_entries = {e.date: e.shift_value for e in entries if e.staff_id == s.id}
            if not staff_entries:
                continue
            staff_rows.append({'staff': s, 'shifts': [(d, staff_entries.get(d, '')) for d in dates]})
        if staff_rows:
            rota_data.append({'section': section, 'staff_rows': staff_rows})

    events_by_date = defaultdict(list)
    for ev in events:
        events_by_date[ev.date].append(ev)

    staffing_rules = list(StaffingRule.objects.select_related('section').all())
    recommendations = {}
    for d in dates:
        evs = events_by_date.get(d, [])
        if evs:
            rec = get_staffing_recommendation(evs, sections, staffing_rules)
            if rec:
                recommendations[d.isoformat()] = rec

    # Hours summary
    hours_data = calculate_hours_summary(entries,
                    [row['staff'] for sd in rota_data for row in sd['staff_rows']])

    # date → week_start mapping
    weeks = group_by_week(dates)
    date_to_week = {}
    for week in weeks:
        week_start = week[0].isoformat()
        for d in week:
            date_to_week[d.isoformat()] = week_start

    # section_staff_json
    section_staff_map = {
        str(sd['section'].id): [row['staff'].id for row in sd['staff_rows']]
        for sd in rota_data
    }

    # Convert hours_data to JSON-safe form
    hours_json_data = {
        str(sid): {wk: mins for wk, mins in data['weekly'].items()}
        for sid, data in hours_data.items()
    }

    import json as _json
    rec_json = _json.dumps({
        k: {
            'tier': v['tier'],
            'total_pax': v['total_pax'],
            'sections': [
                {'section_name': s['section'].name, 'staff_count': s['staff_count'], 'shift': s['shift']}
                for s in v['sections']
            ],
        }
        for k, v in recommendations.items()
    })

    # Borrowed-section labels for rota view
    borrowed_map = {
        (e.staff_id, e.date.isoformat()): e.borrowed_from_section
        for e in entries if getattr(e, 'borrowed_from_section', '')
    }
    # Shift labels (section label shown under each working shift cell)
    cell_labels_map = {
        (e.staff_id, e.date.isoformat()): e.notes
        for e in entries if getattr(e, 'notes', '')
    }
    cell_labels_json = _json.dumps({
        f"{sid}|{diso}": lbl
        for (sid, diso), lbl in cell_labels_map.items()
    })

    # Monthly ledger summary for this period's staff
    months_in_period = sorted(set(
        d.strftime('%Y-%m') for d in dates
    ))
    ledger_entries = MonthlyHoursLedger.objects.filter(
        staff__in=[r['staff'] for sd in rota_data for r in sd['staff_rows']],
        month__in=months_in_period,
    ).select_related('staff')
    ledger_map = {
        (le.staff_id, le.month): le
        for le in ledger_entries
    }
    ledger_json = _json.dumps({
        f"{sid}_{month}": {
            'net_minutes': le.net_minutes,
            'holiday_minutes': le.holiday_minutes,
            'target_minutes': le.target_minutes,
            'status': le.status,
            'total_minutes': le.total_minutes,
            'variance_minutes': le.variance_minutes,
        }
        for (sid, month), le in ledger_map.items()
    })

    return render(request, 'rota/view_rota.html', {
        'period': period,
        'dates': dates,
        'rota_data': rota_data,
        'events_by_date': dict(events_by_date),
        'recommendations': recommendations,
        'recommendations_json': rec_json,
        'hours_json': _json.dumps(hours_json_data),
        'date_to_week_json': _json.dumps(date_to_week),
        'section_staff_json': _json.dumps(section_staff_map),
        'borrowed_map': borrowed_map,
        'borrowed_map_json': _json.dumps({f"{sid}|{diso}": sec for (sid, diso), sec in borrowed_map.items()}),
        'cell_labels_json': cell_labels_json,
        'ledger_json': ledger_json,
        'months_in_period': months_in_period,
    })


# ─── Highlights / Events ──────────────────────────────────────────────────────

def edit_highlights(request, period_id):
    period = get_object_or_404(RotaPeriod, pk=period_id)
    events = list(Event.objects.filter(rota_period=period).order_by('date', '-pax'))
    sections = list(Section.objects.order_by('order'))
    staffing_rules = list(StaffingRule.objects.select_related('section').all())

    events_by_date = defaultdict(list)
    for ev in events:
        events_by_date[ev.date].append(ev)

    recommendations = {}
    for d, evs in events_by_date.items():
        rec = get_staffing_recommendation(evs, sections, staffing_rules)
        if rec:
            recommendations[d.isoformat()] = rec

    all_dates = []
    cur = period.start_date
    while cur <= period.end_date:
        all_dates.append(cur)
        cur += timedelta(days=1)

    return render(request, 'rota/highlights.html', {
        'period': period, 'events': events,
        'sections': sections, 'recommendations': recommendations,
        'all_dates': all_dates, 'event_types': Event.EVENT_TYPES,
    })


@require_POST
def save_event(request, period_id):
    period = get_object_or_404(RotaPeriod, pk=period_id)
    data = json.loads(request.body)
    event_id = data.get('id')
    try:
        date_obj = date.fromisoformat(data['date'])
        pax = int(data.get('pax', 0))
    except (ValueError, KeyError):
        return JsonResponse({'status': 'error', 'message': 'Invalid date or pax'}, status=400)

    description = data.get('description', '').strip()
    event_type = data.get('event_type') or detect_event_type(description)
    notes = data.get('notes', '')

    if event_id:
        ev = get_object_or_404(Event, pk=event_id, rota_period=period)
        ev.date, ev.description, ev.pax, ev.event_type, ev.notes = date_obj, description, pax, event_type, notes
        ev.save()
    else:
        ev = Event.objects.create(
            rota_period=period, date=date_obj, description=description,
            pax=pax, event_type=event_type, notes=notes,
        )

    sections = list(Section.objects.order_by('order'))
    staffing_rules = list(StaffingRule.objects.select_related('section').all())
    rec = get_staffing_recommendation(
        list(Event.objects.filter(rota_period=period, date=date_obj)),
        sections, staffing_rules
    )
    return JsonResponse({
        'status': 'ok',
        'event': {'id': ev.id, 'date': ev.date.isoformat(),
                  'description': ev.description, 'pax': ev.pax,
                  'event_type': ev.event_type, 'tier': ev.tier},
        'recommendation': _serialise_rec(rec),
    })


@require_POST
def delete_event(request, period_id, event_id):
    period = get_object_or_404(RotaPeriod, pk=period_id)
    ev = get_object_or_404(Event, pk=event_id, rota_period=period)
    event_date = ev.date
    ev.delete()
    sections = list(Section.objects.order_by('order'))
    staffing_rules = list(StaffingRule.objects.select_related('section').all())
    rec = get_staffing_recommendation(
        list(Event.objects.filter(rota_period=period, date=event_date)),
        sections, staffing_rules
    )
    return JsonResponse({'status': 'ok', 'date': event_date.isoformat(),
                         'recommendation': _serialise_rec(rec)})


def _serialise_rec(rec):
    if not rec:
        return None
    return {
        'tier': rec['tier'], 'total_pax': rec['total_pax'], 'peak_pax': rec['peak_pax'],
        'notes': rec['notes'],
        'sections': [
            {'section_name': s['section'].name, 'staff_count': s['staff_count'],
             'shift': s['shift'], 'source': s['source']}
            for s in rec['sections']
        ],
    }


# ─── Staffing Rules ───────────────────────────────────────────────────────────

def staffing_rules_view(request):
    rules = StaffingRule.objects.select_related('section').order_by('event_type', 'pax_min')
    sections = Section.objects.order_by('order')
    event_types = [t[0] for t in Event.EVENT_TYPES]

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'create':
            try:
                StaffingRule.objects.create(
                    event_type=request.POST['event_type'],
                    pax_min=int(request.POST['pax_min']),
                    pax_max=int(request.POST['pax_max']),
                    section_id=int(request.POST['section_id']),
                    staff_count=int(request.POST['staff_count']),
                    shift_suggestion=request.POST.get('shift_suggestion', '0800-1630'),
                    notes=request.POST.get('notes', ''),
                )
                messages.success(request, 'Rule created.')
            except Exception as e:
                messages.error(request, f'Error: {e}')
        elif action == 'delete':
            StaffingRule.objects.filter(pk=request.POST.get('rule_id')).delete()
            messages.success(request, 'Rule deleted.')
        return redirect('staffing_rules')

    return render(request, 'rota/staffing_rules.html', {
        'rules': rules, 'sections': sections, 'event_types': event_types,
    })


# ─── Generate Rota ─────────────────────────────────────────────────────────────

def generate_rota_view(request):
    sections = Section.objects.prefetch_related('staff').order_by('order')

    if request.method == 'POST':
        start_str = request.POST.get('start_date')
        end_str = request.POST.get('end_date')
        label = request.POST.get('label', '')
        holiday_json_str = request.POST.get('holiday_data', '{}')
        use_pax = request.POST.get('use_pax_allocation') == '1'
        use_section_rules = request.POST.get('use_section_rules') != '0'  # on by default
        ref_period_id = request.POST.get('ref_period_id', '')

        try:
            start_date = date.fromisoformat(start_str)
            end_date = date.fromisoformat(end_str)
        except (ValueError, TypeError):
            messages.error(request, 'Invalid dates.')
            return redirect('generate_rota')

        date_list = []
        cur = start_date
        while cur <= end_date:
            date_list.append(cur)
            cur += timedelta(days=1)

        try:
            holiday_raw = json.loads(holiday_json_str)
        except Exception:
            holiday_raw = {}

        holiday_map = {}
        for key, val in holiday_raw.items():
            try:
                sid_str, diso = key.split('__', 1)
                holiday_map[(int(sid_str), diso)] = val
            except Exception:
                pass

        all_staff = list(Staff.objects.filter(is_active=True).select_related('section'))
        patterns = load_patterns_from_db([s.id for s in all_staff])

        with transaction.atomic():
            rota_period = RotaPeriod.objects.create(
                label=label or f"Rota {start_date}",
                start_date=start_date, end_date=end_date,
            )

            # ── Process events from the wizard (events_data JSON field) ──────
            events_by_date = defaultdict(list)
            events_json_raw = request.POST.get('events_data', '[]')
            try:
                wizard_events = json.loads(events_json_raw)
            except Exception:
                wizard_events = []

            for ev_data in wizard_events:
                try:
                    ev_date = date.fromisoformat(ev_data['date'])
                    if not (start_date <= ev_date <= end_date):
                        continue
                    new_ev = Event.objects.create(
                        rota_period=rota_period,
                        date=ev_date,
                        description=ev_data.get('desc', ev_data.get('description', '')),
                        pax=int(ev_data.get('pax', 0)),
                        event_type=ev_data.get('type', ev_data.get('event_type', 'Other')),
                        notes=ev_data.get('notes', ''),
                    )
                    events_by_date[ev_date.isoformat()].append(new_ev)
                except (KeyError, ValueError):
                    pass

            # Also copy events from reference period if selected
            if ref_period_id:
                try:
                    ref_events = list(Event.objects.filter(rota_period_id=int(ref_period_id)))
                    for ev in ref_events:
                        if start_date <= ev.date <= end_date:
                            new_ev = Event.objects.create(
                                rota_period=rota_period, date=ev.date,
                                description=ev.description, pax=ev.pax,
                                event_type=ev.event_type, notes=ev.notes,
                            )
                            events_by_date[ev.date.isoformat()].append(new_ev)
                except (ValueError, TypeError):
                    pass

            shift_map, borrowed_labels, assignment_reasons_map, shift_labels_map = generate_rota(
                all_staff, date_list, holiday_map, patterns,
                events_by_date=events_by_date if (use_pax or use_section_rules) else None,
                apply_section_rules=use_section_rules,
            )

            if use_pax:
                recs, borrowed_labels = apply_pax_overrides(
                    shift_map, all_staff, events_by_date,
                    Section.objects.order_by('order'),
                    list(StaffingRule.objects.select_related('section').all()),
                    borrowed_labels,
                )

            entries_to_create = [
                ShiftEntry(
                    rota_period=rota_period, staff_id=sid,
                    date=date.fromisoformat(diso), shift_value=sv, is_generated=True,
                    borrowed_from_section=borrowed_labels.get((sid, diso), ''),
                    notes=shift_labels_map.get((sid, diso), ''),
                )
                for (sid, diso), sv in shift_map.items()
            ]
            ShiftEntry.objects.bulk_create(entries_to_create, ignore_conflicts=True)

            # Update monthly hours ledger for all months touched by this period
            months_covered = sorted(set(
                date.fromisoformat(diso).strftime('%Y-%m')
                for (_, diso) in shift_map
            ))
            for month_str in months_covered:
                update_monthly_ledger(all_staff, month_str)

        messages.success(request, f'Generated "{rota_period.label}" with {len(entries_to_create)} entries.')
        return redirect('view_rota', period_id=rota_period.id)

    staff_list = Staff.objects.filter(is_active=True).select_related('section').order_by('section__order', 'name')
    recent_periods = RotaPeriod.objects.order_by('-start_date')[:8]
    pattern_count = ShiftPattern.objects.count()
    return render(request, 'rota/generate.html', {
        'sections': sections, 'staff_list': staff_list,
        'today': date.today().isoformat(),
        'recent_periods': recent_periods,
        'pattern_count': pattern_count,
    })


# ─── Staff Management ─────────────────────────────────────────────────────────

def staff_manage(request):
    sections = list(Section.objects.order_by('order'))
    # Annotate sections with colour
    for sec in sections:
        sec.color = _section_color(sec.name)

    sections_with_staff = []
    for sec in sections:
        members = list(Staff.objects.filter(section=sec).order_by('name'))
        if members:
            sections_with_staff.append((sec, members))

    return render(request, 'rota/staff_manage.html', {
        'sections': sections,
        'sections_with_staff': sections_with_staff,
    })


@require_POST
def save_staff(request):
    """AJAX: create or update a Staff record."""
    data = json.loads(request.body)
    staff_id = data.get('id')
    name = (data.get('name') or '').strip()
    if not name:
        return JsonResponse({'status': 'error', 'message': 'Name is required'}, status=400)

    section_id = data.get('section_id')
    defaults = {
        'name': name,
        'role': data.get('role', ''),
        'section_id': section_id,
        'employment_type': data.get('employment_type', 'full_time'),
        'contracted_hours_pw': float(data.get('contracted_hours_pw', 40)),
        'email': data.get('email', ''),
        'phone': data.get('phone', ''),
        'is_active': data.get('is_active', True),
        'start_date': data.get('start_date') or None,
        'end_date': data.get('end_date') or None,
    }

    if staff_id:
        Staff.objects.filter(pk=staff_id).update(**defaults)
        staff = Staff.objects.get(pk=staff_id)
    else:
        staff = Staff.objects.create(**defaults)

    return JsonResponse({'status': 'ok', 'id': staff.id, 'name': staff.name})


@require_POST
def delete_staff(request, staff_id):
    """AJAX: delete a staff member and all their entries."""
    staff = get_object_or_404(Staff, pk=staff_id)
    name = staff.name
    staff.delete()
    return JsonResponse({'status': 'ok', 'name': name})


@require_POST
def toggle_staff_active(request, staff_id):
    """AJAX: toggle is_active on a staff record."""
    staff = get_object_or_404(Staff, pk=staff_id)
    data = json.loads(request.body)
    staff.is_active = bool(data.get('is_active', True))
    staff.save()
    return JsonResponse({'status': 'ok', 'is_active': staff.is_active})


def _section_color(section_name):
    name = (section_name or '').upper()
    for keyword, color in SECTION_COLORS.items():
        if keyword.upper() in name or name in keyword.upper():
            return color
    return '#888888'


# ─── Edit Rota Cell (AJAX) ─────────────────────────────────────────────────────

@ensure_csrf_cookie
@require_POST
def update_shift(request):
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, Exception) as e:
        return JsonResponse({'status': 'error', 'message': f'Invalid JSON: {e}'}, status=400)

    period_id  = data.get('period_id')
    staff_id   = data.get('staff_id')
    date_str   = data.get('date')
    shift_value = data.get('shift_value', '').strip()

    if not all([period_id, staff_id, date_str]):
        return JsonResponse({'status': 'error', 'message': 'Missing required fields'}, status=400)

    try:
        entry = ShiftEntry.objects.get(
            rota_period_id=period_id,
            staff_id=staff_id,
            date=date_str,
        )
    except ShiftEntry.DoesNotExist:
        # Entry not found — create it (can happen with generated rotas in edge cases)
        try:
            entry = ShiftEntry.objects.create(
                rota_period_id=period_id,
                staff_id=staff_id,
                date=date_str,
                shift_value=shift_value,
                is_generated=True,
            )
            return JsonResponse({'status': 'ok', 'old_value': '', 'new_value': shift_value})
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)}, status=500)
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=500)

    old_value = entry.shift_value
    entry.shift_value = shift_value
    entry.save()
    # Learn from manual corrections (real rotas only, not generated)
    if not entry.is_generated:
        save_patterns_to_db([entry])
    # Refresh the monthly hours ledger for the affected month
    try:
        month_str = entry.date.strftime('%Y-%m')
        all_staff_list = list(Staff.objects.filter(is_active=True))
        update_monthly_ledger(all_staff_list, month_str)
    except Exception:
        pass  # ledger update is best-effort — never fail a cell edit
    return JsonResponse({'status': 'ok', 'old_value': old_value, 'new_value': shift_value})



def events_json_api(request, period_id):
    """Return events for a period as JSON — used by the generate page copy feature."""
    period = get_object_or_404(RotaPeriod, pk=period_id)
    events = Event.objects.filter(rota_period=period).order_by('date', '-pax')
    return JsonResponse({
        'status': 'ok',
        'period': str(period),
        'events': [
            {
                'id': ev.id,
                'date': ev.date.isoformat(),
                'description': ev.description,
                'pax': ev.pax,
                'event_type': ev.event_type,
                'notes': ev.notes,
            }
            for ev in events
        ]
    })


# ─── WHY API ──────────────────────────────────────────────────────────────────

def shift_why(request):
    """
    GET /api/shift/why/?period_id=X&staff_id=Y&date=YYYY-MM-DD

    Returns the recorded reasons for why a shift was assigned.
    Reasons are stored in memory during rota generation (reset each run).
    If no reasons are found (e.g. for an imported rota), returns a
    'IMPORTED' reason explaining the shift came from an external file.
    """
    period_id = request.GET.get('period_id')
    staff_id  = request.GET.get('staff_id')
    date_str  = request.GET.get('date')

    if not all([period_id, staff_id, date_str]):
        return JsonResponse({'status': 'error', 'message': 'period_id, staff_id and date required'}, status=400)

    try:
        entry = ShiftEntry.objects.select_related('staff', 'staff__section').get(
            rota_period_id=int(period_id),
            staff_id=int(staff_id),
            date=date_str,
        )
    except ShiftEntry.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Shift entry not found'}, status=404)

    # Get reasons from the module-level store
    reasons_raw = get_assignment_reasons().get((int(staff_id), date_str), [])

    if not reasons_raw:
        # Imported or manually-edited rota — provide a contextual explanation
        if entry.is_generated:
            reasons = [{'code': 'GENERATED', 'label': 'Algorithm-generated',
                        'explanation': 'This shift was assigned by the rota algorithm.',
                        'severity': 'info'}]
        else:
            reasons = [{'code': 'IMPORTED', 'label': 'Imported from Excel',
                        'explanation': 'This shift was imported from an external rota file.',
                        'severity': 'info'}]
    else:
        reasons = [
            {'code': r.code, 'label': r.label,
             'explanation': r.explanation, 'severity': r.severity}
            for r in reasons_raw
        ]

    # Find same-section colleagues who were not chosen (for context)
    section = entry.staff.section
    alternatives = []
    if section:
        colleagues = Staff.objects.filter(section=section, is_active=True).exclude(pk=entry.staff_id)[:5]
        for c in colleagues:
            try:
                c_entry = ShiftEntry.objects.get(
                    rota_period_id=int(period_id),
                    staff_id=c.id,
                    date=date_str,
                )
                if c_entry.shift_value in ('OFF', 'H', 'SICK'):
                    alternatives.append({'staff': c.name, 'status': c_entry.shift_value})
            except ShiftEntry.DoesNotExist:
                pass

    return JsonResponse({
        'status':  'ok',
        'staff':   entry.staff.name,
        'staff_id': entry.staff.id,
        'date':    date_str,
        'shift':   entry.shift_value,
        'section': section.name if section else None,
        'is_generated': entry.is_generated,
        'locked_by_manager': not entry.is_generated,
        'reasons': reasons,
        'colleagues_on_rest': alternatives,
    })

# ─── Export ────────────────────────────────────────────────────────────────────

def export_rota(request, period_id):
    period = get_object_or_404(RotaPeriod, pk=period_id)
    entries = ShiftEntry.objects.filter(rota_period=period).select_related('staff', 'staff__section')
    dates = sorted(set(e.date for e in entries))
    shift_map = {(e.staff_id, e.date.isoformat()): e.shift_value for e in entries}

    sections_with_staff = []
    for section in Section.objects.order_by('order'):
        members = [s for s in Staff.objects.filter(section=section, is_active=True)
                   if any((s.id, d.isoformat()) in shift_map for d in dates)]
        if members:
            sections_with_staff.append((section, members))

    # Build events_by_date for highlights row in Excel
    from collections import defaultdict as _dd
    events_export = _dd(list)
    for ev in Event.objects.filter(rota_period=period).order_by('date', '-pax'):
        events_export[ev.date].append(ev)
    # Build borrowed labels map from saved ShiftEntry.borrowed_from_section
    borrowed_export = {
        (e.staff_id, e.date.isoformat()): e.borrowed_from_section
        for e in entries if e.borrowed_from_section
    }
    # Build shift_labels from ShiftEntry.notes
    labels_export = {
        (e.staff_id, e.date.isoformat()): e.notes
        for e in entries if getattr(e, 'notes', '')
    }
    buf = export_rota_to_xlsx(period, sections_with_staff, shift_map, dates,
                               dict(events_export), borrowed_export, labels_export)
    response = HttpResponse(
        buf.getvalue(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = f'attachment; filename="rota_{period.label.replace(" ", "_")}.xlsx"'
    return response


# ─── Staff list / patterns ────────────────────────────────────────────────────

def staff_list(request):
    sections = Section.objects.prefetch_related('staff').order_by('order')
    return render(request, 'rota/staff_list.html', {'sections': sections})


def staff_patterns(request, staff_id):
    staff = get_object_or_404(Staff, pk=staff_id)
    patterns = ShiftPattern.objects.filter(staff=staff).order_by('day_of_week', '-frequency')
    days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
    by_day = defaultdict(list)
    for p in patterns:
        by_day[days[p.day_of_week]].append(p)
    return render(request, 'rota/staff_patterns.html', {
        'staff': staff, 'by_day': dict(by_day), 'days': days,
    })
