from celery import shared_task


@shared_task(ignore_result=True)
def run_agent_task(group_id, member_id=None, event=None, message_id=None):
    from chat.services import run_agent
    run_agent(group_id, member_id, event, message_id)
