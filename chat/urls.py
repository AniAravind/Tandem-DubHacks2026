from django.urls import path
from chat import api
from chat.photon import bridge
from chat import registration

urlpatterns = [
    path("demo/registrations", registration.register),
    path("demo/registrations/status", registration.status),
    path("chat/photon", bridge),
    path("chat/web", api.chat_web),
    path("groups/<uuid:gid>/messages", api.group_messages),
    path("groups/<uuid:gid>/chat/demo/share", api.demo_share),          # fake engine only (DEMO_MODE)
]
