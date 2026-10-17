from django.urls import path
from chat import api

urlpatterns = [
    path("chat/web", api.chat_web),
    path("groups/<uuid:gid>/messages", api.group_messages),
]
