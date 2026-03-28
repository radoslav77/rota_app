from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name='Section',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100, unique=True)),
                ('order', models.IntegerField(default=0)),
            ],
            options={'ordering': ['order']},
        ),
        migrations.CreateModel(
            name='Staff',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=150)),
                ('role', models.CharField(blank=True, max_length=100)),
                ('is_active', models.BooleanField(default=True)),
                ('typical_shifts', models.TextField(default='{}', help_text='JSON: day_of_week -> preferred shift')),
                ('section', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='staff', to='rota.section')),
            ],
        ),
        migrations.CreateModel(
            name='RotaPeriod',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('label', models.CharField(max_length=100)),
                ('start_date', models.DateField()),
                ('end_date', models.DateField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('highlights', models.TextField(blank=True)),
            ],
            options={'ordering': ['-start_date']},
        ),
        migrations.CreateModel(
            name='ShiftEntry',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('date', models.DateField()),
                ('shift_value', models.CharField(max_length=30)),
                ('is_generated', models.BooleanField(default=False)),
                ('notes', models.CharField(blank=True, max_length=200)),
                ('rota_period', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='entries', to='rota.rotaperiod')),
                ('staff', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='entries', to='rota.staff')),
            ],
            options={'ordering': ['date', 'staff__section__order'], 'unique_together': {('rota_period', 'staff', 'date')}},
        ),
        migrations.CreateModel(
            name='ShiftPattern',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('day_of_week', models.IntegerField()),
                ('shift_value', models.CharField(max_length=30)),
                ('frequency', models.IntegerField(default=1)),
                ('last_seen', models.DateField(blank=True, null=True)),
                ('staff', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='patterns', to='rota.staff')),
            ],
            options={'ordering': ['-frequency'], 'unique_together': {('staff', 'day_of_week', 'shift_value')}},
        ),
    ]
