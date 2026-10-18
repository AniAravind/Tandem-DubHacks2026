from django.contrib import admin
from django.urls import include, path, re_path
from tandem.views import health, live, spa_index
from merchant.views import agent_keys

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/health", health),  # compatibility: readiness
    path("api/live", live),
    path("api/ready", health),
    path("api/v1/", include("chat.urls")),             # register before generic demo/<action>
    path("api/v1/", include("purchases.urls")),        # groups, options, votes, purchases, shares, expenses, ledger, events, demo
    path("api/v1/", include("payments.urls")),         # consent/*
    path("merchants/", include("merchant.urls")),      # mock merchants + /.well-known keys
    path(".well-known/tandem-agent-keys", agent_keys),                   # public Ed25519 keys (simulated agent directory)
    re_path(r"^(?!api/|admin/|static/|merchants/|\.well-known/).*$", spa_index),  # React routes
]
