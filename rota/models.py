from django.db import models
import json


class Section(models.Model):
    name = models.CharField(max_length=100, unique=True)
    order = models.IntegerField(default=0)

    class Meta:
        ordering = ['order']

    def __str__(self):
        return self.name


class Staff(models.Model):
    EMPLOYMENT_TYPES = [
        ('full_time', 'Full Time'),
        ('part_time', 'Part Time'),
        ('agency', 'Agency'),
        ('zero_hours', 'Zero Hours'),
    ]

    name = models.CharField(max_length=150)
    role = models.CharField(max_length=100, blank=True)
    section = models.ForeignKey(Section, on_delete=models.SET_NULL, null=True, related_name='staff')
    is_active = models.BooleanField(default=True)
    typical_shifts = models.TextField(default='{}', help_text='JSON: day_of_week -> preferred shift')
    contracted_hours_pw = models.DecimalField(
        max_digits=5, decimal_places=2, default=40.0,
        help_text='Contracted hours per week'
    )
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=30, blank=True)
    employment_type = models.CharField(
        max_length=20, default='full_time', choices=EMPLOYMENT_TYPES
    )

    def __str__(self):
        return self.name

    def get_typical_shifts(self):
        try:
            return json.loads(self.typical_shifts)
        except Exception:
            return {}

    def set_typical_shifts(self, data):
        self.typical_shifts = json.dumps(data)


class RotaPeriod(models.Model):
    label = models.CharField(max_length=100)
    start_date = models.DateField()
    end_date = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)
    highlights = models.TextField(blank=True)

    class Meta:
        ordering = ['-start_date']

    def __str__(self):
        return f"{self.label} ({self.start_date} – {self.end_date})"


class ShiftEntry(models.Model):
    STATUS_CHOICES = [
        ('shift', 'Shift'),
        ('OFF', 'OFF'),
        ('H', 'Holiday'),
        ('SICK', 'Sick'),
        ('Comp', 'Comp Day'),
        ('Paternity', 'Paternity'),
        ('Maternity', 'Maternity'),
        ('TBC', 'TBC'),
        ('OFF/R', 'OFF/R'),
    ]

    rota_period = models.ForeignKey(RotaPeriod, on_delete=models.CASCADE, related_name='entries')
    staff = models.ForeignKey(Staff, on_delete=models.CASCADE, related_name='entries')
    date = models.DateField()
    shift_value = models.CharField(max_length=30)  # e.g. "0800-1630", "OFF", "H"
    is_generated = models.BooleanField(default=False)
    notes = models.CharField(max_length=200, blank=True)
    borrowed_from_section = models.CharField(
        max_length=100, blank=True,
        help_text='Original section name if this chef was moved to cover another section'
    )

    class Meta:
        unique_together = ('rota_period', 'staff', 'date')
        ordering = ['date', 'staff__section__order']

    def __str__(self):
        return f"{self.staff.name} | {self.date} | {self.shift_value}"

    @property
    def is_working(self):
        return self.shift_value not in ('OFF', 'H', 'SICK', 'Comp', 'Paternity', 'Maternity', 'TBC', 'OFF/R', '')

    @property
    def status_type(self):
        if self.is_working:
            return 'shift'
        return self.shift_value


class Event(models.Model):
    """A specific event or function on a date within a rota period."""
    EVENT_TYPES = [
        ('DDR', 'DDR (Day Delegate Rate)'),
        ('Reception', 'Reception'),
        ('Dinner', 'Dinner'),
        ('Lunch', 'Lunch'),
        ('Canapes', 'Canapés'),
        ('Buffet', 'Buffet'),
        ('Breakfast', 'Breakfast'),
        ('Tasting', 'Menu Tasting'),
        ('Other', 'Other'),
    ]

    rota_period = models.ForeignKey(RotaPeriod, on_delete=models.CASCADE, related_name='events')
    date = models.DateField()
    description = models.CharField(max_length=300)
    pax = models.IntegerField(default=0, help_text='Total number of guests/covers')
    event_type = models.CharField(max_length=50, blank=True, choices=EVENT_TYPES)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ['date', '-pax']

    def __str__(self):
        return f"{self.date} | {self.description} ({self.pax} pax)"

    @property
    def tier(self):
        """Returns staffing tier based on pax count."""
        if self.pax == 0:
            return 'tasting'
        elif self.pax <= 30:
            return 'small'
        elif self.pax <= 100:
            return 'medium'
        elif self.pax <= 250:
            return 'large'
        else:
            return 'vip'


class StaffingRule(models.Model):
    """Manager-defined rule: for event_type with pax_min..pax_max guests,
    recommend staff_count people from this section on shift_suggestion."""
    event_type = models.CharField(max_length=50, help_text='e.g. DDR, Reception, Dinner')
    pax_min = models.IntegerField(default=0)
    pax_max = models.IntegerField(default=9999)
    section = models.ForeignKey(Section, on_delete=models.CASCADE, related_name='rules')
    staff_count = models.IntegerField(help_text='Recommended number of staff from this section')
    shift_suggestion = models.CharField(max_length=30, default='0800-1630')
    notes = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ['event_type', 'pax_min']
        unique_together = ('event_type', 'pax_min', 'pax_max', 'section')

    def __str__(self):
        return f"{self.event_type} {self.pax_min}-{self.pax_max}pax → {self.section.name}: {self.staff_count} staff"



class MonthlyHoursLedger(models.Model):
    """
    Tracks each staff member's actual vs target hours per calendar month.
    Updated whenever a rota period is generated or shift entries are changed.
    Holidays (H) count as 8 paid hours per day.
    """
    STATUS_CHOICES = [
        ('ok',    'On target'),
        ('under', 'Under hours'),
        ('over',  'Over hours'),
    ]

    staff = models.ForeignKey(Staff, on_delete=models.CASCADE, related_name='monthly_hours')
    month = models.CharField(max_length=7, help_text='YYYY-MM')
    net_minutes = models.IntegerField(
        default=0,
        help_text='Actual net paid minutes from working shifts (breaks deducted)'
    )
    holiday_minutes = models.IntegerField(
        default=0,
        help_text='Minutes from H days counted at 8 h each'
    )
    target_minutes = models.IntegerField(
        default=10410,
        help_text='Monthly target in minutes — 173.5 h × 60 for full-time'
    )
    status = models.CharField(
        max_length=10, default='ok', choices=STATUS_CHOICES
    )
    last_updated = models.DateTimeField(auto_now=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ['-month', 'staff__name']
        unique_together = ('staff', 'month')

    def __str__(self):
        total = self.net_minutes + self.holiday_minutes
        return (f"{self.staff.name} | {self.month} | "
                f"{total//60}h{total%60:02d}m / {self.target_minutes//60}h{self.target_minutes%60:02d}m "
                f"[{self.status}]")

    @property
    def total_minutes(self):
        return self.net_minutes + self.holiday_minutes

    @property
    def variance_minutes(self):
        """Positive = over target, negative = under target."""
        return self.total_minutes - self.target_minutes

class ShiftPattern(models.Model):
    """Learned shift patterns per staff per day-of-week"""
    staff = models.ForeignKey(Staff, on_delete=models.CASCADE, related_name='patterns')
    day_of_week = models.IntegerField()  # 0=Mon, 6=Sun
    shift_value = models.CharField(max_length=30)
    frequency = models.IntegerField(default=1)
    last_seen = models.DateField(null=True, blank=True)

    class Meta:
        unique_together = ('staff', 'day_of_week', 'shift_value')
        ordering = ['-frequency']

    def __str__(self):
        days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
        return f"{self.staff.name} | {days[self.day_of_week]} | {self.shift_value} (x{self.frequency})"
