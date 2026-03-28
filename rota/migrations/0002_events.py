from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('rota', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='Event',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('rota_period', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='events', to='rota.rotaperiod')),
                ('date', models.DateField()),
                ('description', models.CharField(max_length=300)),
                ('pax', models.IntegerField(default=0, help_text='Total number of guests/covers')),
                ('event_type', models.CharField(max_length=50, blank=True, help_text='e.g. DDR, Reception, Dinner, Lunch, Canapes')),
                ('notes', models.TextField(blank=True)),
            ],
            options={
                'ordering': ['date', '-pax'],
            },
        ),
        migrations.CreateModel(
            name='StaffingRule',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('event_type', models.CharField(max_length=50, help_text='e.g. DDR, Reception, Dinner, Canapes, Lunch')),
                ('pax_min', models.IntegerField(default=0)),
                ('pax_max', models.IntegerField(default=9999)),
                ('section', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='rules', to='rota.section')),
                ('staff_count', models.IntegerField(help_text='Recommended number of staff from this section')),
                ('shift_suggestion', models.CharField(max_length=30, default='0800-1630', help_text='Suggested shift time for this event type')),
                ('notes', models.CharField(max_length=200, blank=True)),
            ],
            options={
                'ordering': ['event_type', 'pax_min'],
                'unique_together': {('event_type', 'pax_min', 'pax_max', 'section')},
            },
        ),
    ]
