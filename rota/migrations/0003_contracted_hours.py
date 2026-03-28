from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('rota', '0002_events'),
    ]

    operations = [
        migrations.AddField(
            model_name='staff',
            name='contracted_hours_pw',
            field=models.DecimalField(
                max_digits=5, decimal_places=2, default=40.0,
                help_text='Contracted hours per week (default 40)'
            ),
        ),
        migrations.AddField(
            model_name='staff',
            name='start_date',
            field=models.DateField(null=True, blank=True,
                help_text='Date this chef joined / started at the hotel'),
        ),
        migrations.AddField(
            model_name='staff',
            name='end_date',
            field=models.DateField(null=True, blank=True,
                help_text='Date this chef left (blank = still employed)'),
        ),
        migrations.AddField(
            model_name='staff',
            name='email',
            field=models.EmailField(blank=True, max_length=254),
        ),
        migrations.AddField(
            model_name='staff',
            name='phone',
            field=models.CharField(max_length=30, blank=True),
        ),
        migrations.AddField(
            model_name='staff',
            name='employment_type',
            field=models.CharField(
                max_length=20, default='full_time',
                choices=[
                    ('full_time', 'Full Time'),
                    ('part_time', 'Part Time'),
                    ('agency', 'Agency'),
                    ('zero_hours', 'Zero Hours'),
                ]
            ),
        ),
    ]
