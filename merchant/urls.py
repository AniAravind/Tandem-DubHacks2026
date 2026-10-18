from django.urls import path
from merchant import views

# Mounted at /merchants/ in tandem/urls.py. Rishab replaces the stub views with the real mock merchant + verifier.
urlpatterns = [
    path("<slug:slug>/listings", views.listings),
    path("<slug:slug>/quotes", views.quotes),
    path("<slug:slug>/agent/checkout", views.agent_checkout),
    path("<slug:slug>/agent/cancel", views.agent_cancel),
]
