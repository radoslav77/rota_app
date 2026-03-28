from django.urls import path
from . import views

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('import/', views.import_rota, name='import_rota'),
    path('generate/', views.generate_rota_view, name='generate_rota'),
    path('rota/<int:period_id>/', views.view_rota, name='view_rota'),
    path('rota/<int:period_id>/export/', views.export_rota, name='export_rota'),
    path('rota/<int:period_id>/highlights/', views.edit_highlights, name='edit_highlights'),
    path('rota/<int:period_id>/events/save/', views.save_event, name='save_event'),
    path('rota/<int:period_id>/events/<int:event_id>/delete/', views.delete_event, name='delete_event'),
    path('api/shift/update/', views.update_shift, name='update_shift'),
    path('staff/', views.staff_list, name='staff_list'),
    path('staff/manage/', views.staff_manage, name='staff_manage'),
    path('staff/save/', views.save_staff, name='save_staff'),
    path('staff/<int:staff_id>/delete/', views.delete_staff, name='delete_staff'),
    path('staff/<int:staff_id>/toggle-active/', views.toggle_staff_active, name='toggle_staff_active'),
    path('staff/<int:staff_id>/patterns/', views.staff_patterns, name='staff_patterns'),
    path('staffing-rules/', views.staffing_rules_view, name='staffing_rules'),
    path('rota/<int:period_id>/events/json/', views.events_json_api, name='events_json'),
    path('api/shift/why/', views.shift_why, name='shift_why'),
]
