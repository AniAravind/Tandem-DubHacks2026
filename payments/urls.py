from django.urls import path
from payments import api

urlpatterns = [
    path("consent/<str:token>", api.consent_detail),
    path("consent/<str:token>/tokenize", api.consent_tokenize),
    path("consent/<str:token>/approve", api.consent_approve),
    path("consent/<str:token>/decline", api.consent_decline),
    path("shares/<uuid:sid>/reconfirm", api.share_reconfirm),
]
