from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('rota', '0003_contracted_hours'),
    ]

    operations = [
        migrations.AddField(
            model_name='shiftentry',
            name='borrowed_from_section',
            field=models.CharField(
                max_length=100, blank=True,
                help_text='Original section name if this chef was moved to cover another section'
            ),
        ),
        migrations.CreateModel(
            name='MonthlyHoursLedger',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('staff', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                    related_name='monthly_hours', to='rota.staff')),
                ('month', models.CharField(max_length=7,
                    help_text='YYYY-MM format')),
                ('net_minutes', models.IntegerField(default=0,
                    help_text='Actual net paid minutes worked (breaks deducted)')),
                ('holiday_minutes', models.IntegerField(default=0,
                    help_text='Minutes counted from H (holiday) days at 8h each')),
                ('target_minutes', models.IntegerField(default=10410,
                    help_text='Monthly target in minutes (173.5h = 10410)')),
                ('status', models.CharField(max_length=10, default='ok',
                    choices=[('ok', 'On target'), ('under', 'Under hours'), ('over', 'Over hours')],
                    help_text='Comparison of actual vs target')),
                ('last_updated', models.DateTimeField(auto_now=True)),
                ('notes', models.TextField(blank=True)),
            ],
            options={
                'ordering': ['-month', 'staff__name'],
                'unique_together': {('staff', 'month')},
            },
        ),
    ]
