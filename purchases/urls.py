from django.urls import path
from purchases import api
from purchases import plans

urlpatterns = [
    path("plans/<uuid:plan_id>", plans.detail),
    path("plans/<uuid:plan_id>/join", plans.join),
    path("plans/<uuid:plan_id>/fund", plans.fund),
    path("groups/<uuid:gid>", api.group_detail),
    path("groups/<uuid:gid>/members/me/preferences", api.my_preferences),
    path("groups/<uuid:gid>/options", api.options),
    path("votes/<uuid:vid>/ballots", api.cast_ballot),
    path("purchases/<uuid:pid>", api.purchase_detail),
    path("shares/<uuid:sid>/leave", api.leave_share),
    path("groups/<uuid:gid>/expenses", api.add_expense),
    path("groups/<uuid:gid>/ledger", api.ledger),
    path("groups/<uuid:gid>/events", api.events),
    path("demo/<str:action>", api.demo_action),
]
