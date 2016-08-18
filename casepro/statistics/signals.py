from __future__ import unicode_literals

from django.db.models.signals import post_save, m2m_changed
from django.dispatch import receiver

from casepro.msgs.models import Message, Label, Outgoing
from casepro.cases.models import Case, CaseAction

from .models import datetime_to_date, DailyCount


@receiver(post_save, sender=Message)
def record_new_incoming(sender, instance, created, **kwargs):
    """
    Records a new outgoing being sent
    """
    if created:
        org = instance.org

        # get day in org timezone
        day = datetime_to_date(instance.created_on, org)

        DailyCount.record_item(day, DailyCount.TYPE_INCOMING, org)


@receiver(post_save, sender=Outgoing)
def record_new_outgoing(sender, instance, created, **kwargs):
    if created and instance.is_reply():
        org = instance.org
        partner = instance.partner
        user = instance.created_by

        # get day in org timezone
        day = datetime_to_date(instance.created_on, org)

        DailyCount.record_item(day, DailyCount.TYPE_REPLIES, org)
        DailyCount.record_item(day, DailyCount.TYPE_REPLIES, org, user)

        if instance.partner:
            DailyCount.record_item(day, DailyCount.TYPE_REPLIES, partner)


@receiver(m2m_changed, sender=Message.labels.through)
def record_incoming_labelling(sender, instance, action, reverse, model, pk_set, **kwargs):
    day = datetime_to_date(instance.created_on, instance.org)

    if action == 'post_add':
        for label_id in pk_set:
            DailyCount.record_item(day, DailyCount.TYPE_INCOMING, Label(pk=label_id))
    elif action == 'post_remove':
        for label_id in pk_set:
            DailyCount.record_removal(day, DailyCount.TYPE_INCOMING, Label(pk=label_id))
    elif action == 'pre_clear':
        for label in instance.labels.all():
            DailyCount.record_removal(day, DailyCount.TYPE_INCOMING, label)


@receiver(post_save, sender=Case)
def record_new_case(sender, instance, created, **kwargs):
    day = datetime_to_date(instance.opened_on, instance.org)
    if instance.closed_on:
        DailyCount.record_item(day, DailyCount.TYPE_CASE_CLOSED, instance.assignee)
    else:
        DailyCount.record_item(day, DailyCount.TYPE_CASE_OPENED, instance.assignee)


@receiver(post_save, sender=CaseAction)
def record_new_case_action(sender, instance, created, **kwargs):
    org = instance.case.org
    user = instance.created_by

    day = datetime_to_date(instance.created_on, instance.case.org)
    if instance.action in [CaseAction.OPEN, CaseAction.REOPEN]:
        DailyCount.record_item(day, DailyCount.TYPE_CASE_OPENED, org, user)
    elif instance.action == CaseAction.REASSIGN:

        DailyCount.record_item(day, DailyCount.TYPE_CASE_OPENED, org, user)

        previous_user = CaseAction.objects.filter(
            case=instance.case).order_by('-created_by').first()
        if previous_user:
            DailyCount.record_item(
                day, DailyCount.TYPE_CASE_CLOSED, org, previous_user)
    elif instance.action == CaseAction.CLOSE:
        DailyCount.record_item(
            day, DailyCount.TYPE_CASE_CLOSED, org, user)
