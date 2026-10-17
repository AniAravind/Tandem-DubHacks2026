from django.urls import path
from merchant import views

# Mounted at /merchants/ in tandem/urls.py. Rishab replaces the stub views with the real mock merchant + verifier.
urlpatterns = [
    path("<slug:slug>/listings", views.not_implemented),
    path("<slug:slug>/quotes", views.not_implemented),
    path("<slug:slug>/agent/checkout", views.not_implemented),
    path("<slug:slug>/agent/cancel", views.not_implemented),
]
